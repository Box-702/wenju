# =====================================================================
# chat.py —— 对话式 Agent 与会话管理
#
# 一轮对话（非流式 / SSE 流式）、历史消息，以及「一个项目下多个独立
# 对话 / 全局对话」的增删改查。编排逻辑在 app/chat/conductor.py。
# =====================================================================

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..chat import conductor as chat_svc
from . import common, deps
from .schemas import ChatRequest, ConversationCreate, ConversationRename

router = APIRouter()

# 对话图片上传限制：单张 ≤10MB，单条消息 ≤4 张（上下文与请求体都扛得住）。
_CHAT_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_CHAT_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
_CHAT_IMAGE_MAX_COUNT = 4


@router.post("/chat/images")
async def upload_chat_image(file: UploadFile = File(...)) -> dict[str, Any]:
    """上传对话用图片（字节入库，不落盘），返回 id 与回放 url。

    前端在发送消息前先传图拿 id，再把 id 列表放进 /chat/stream 的 images 字段。
    """
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in _CHAT_IMAGE_TYPES:
        raise HTTPException(400, "仅支持 png / jpg / webp / gif 图片")
    data = await file.read()
    if len(data) > _CHAT_IMAGE_MAX_BYTES:
        raise HTTPException(400, "图片不能超过 10MB")
    if not data:
        raise HTTPException(400, "空文件")
    img = deps.store().save_chat_image(thread_id="", mime=mime, name=file.filename or "", content=data)
    return {"id": img.id, "url": f"/api/chat/images/{img.id}", "mime": mime, "name": img.name}


class SkillImport(BaseModel):
    """导入一个技能：.md 全文（frontmatter + 正文）。"""

    content: str = Field(default="", min_length=1)
    filename: str = Field(default="")


@router.post("/skills")
def import_skill(payload: SkillImport) -> dict[str, Any]:
    """导入一个技能。

    解析规则与内置技能完全一致（同一份 frontmatter 规范）：名字冲突时
    内置技能优先——内置技能是随代码走的官方能力，被用户同名抢走会让它失效。
    """
    from ..skills import parse_skill_text
    from ..skills.loader import SKILLS_DIR

    try:
        skill = parse_skill_text(payload.content, name_hint=payload.filename)
    except ValueError as e:
        raise HTTPException(400, f"技能解析失败：{e}") from None

    # 内置技能同名 → 拒绝（提示换个名字），不要覆盖官方能力
    builtin = (SKILLS_DIR / skill.meta.name / "SKILL.md").is_file()
    if builtin:
        raise HTTPException(409, f"「{skill.meta.name}」是内置技能，换个名字再导入")

    store = deps.store()
    store.save_user_skill(
        name=skill.meta.name,
        description=skill.meta.description,
        when_to_use=skill.meta.when_to_use,
        audience=skill.meta.audience,
        content=payload.content,
    )
    return {
        "ok": True,
        "name": skill.meta.name,
        "description": skill.meta.description,
        "when_to_use": skill.meta.when_to_use,
        "audience": skill.meta.audience,
    }


@router.delete("/skills/{name}")
def delete_skill(name: str) -> dict[str, Any]:
    """删除一个用户导入的技能（内置技能不受影响）。"""
    store = deps.store()
    removed = store.delete_user_skill(name)
    if not removed:
        raise HTTPException(404, f"没有这个用户技能：{name}")
    return {"ok": True, "name": name, "removed": True}


@router.get("/skills")
def list_skills_api(audience: str = "main") -> dict[str, Any]:
    """列出可用技能（默认只列主 Agent 能加载的）。

    侧边栏的「技能」段用它渲染；用户手动点一个技能，等于在输入框里预填
    它的适用场景，Agent 看到就会去 load_skill。
    """
    from ..skills import list_skills

    metas = list_skills(audience=audience or None)
    return {
        "skills": [
            {
                "name": m.name,
                "description": m.description,
                "when_to_use": m.when_to_use,
                "audience": m.audience,
            }
            for m in metas
        ]
    }


@router.get("/chat/images/{image_id}")
def get_chat_image(image_id: str) -> Response:
    """回放对话图片字节（前端 <img> 与历史消息都用它）。"""
    img = deps.store().get_chat_image(image_id)
    if img is None:
        raise HTTPException(404, "图片不存在")
    return Response(
        content=img.content,
        media_type=img.mime,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _conversation_dict(conv: Any) -> dict[str, Any]:
    return {
        "id": conv.id,
        "project_id": conv.project_id,
        "title": conv.title,
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
    }


def _resolve_conversation(project_id: str | None, conversation_id: str | None) -> str | None:
    """解析会话线程：显式 conversation_id 优先；否则用项目的默认对话；都没有则全局。"""
    if conversation_id:
        conv = deps.store().get_conversation(conversation_id)
        if not conv:
            raise HTTPException(404, "对话不存在")
        # 归属校验：防止把 A 项目的消息写进 B 项目的对话线程，造成上下文串扰。
        if project_id and conv.project_id and conv.project_id != project_id:
            raise HTTPException(400, "对话不属于该项目")
        return conv.id
    if project_id:
        p = deps.store().get_project(project_id)
        if not p:
            raise HTTPException(404, "项目不存在")
        return deps.store().ensure_default_conversation(project_id).id
    return None


# ---------- 对话 ----------


def _acquire_turn_thread(payload: ChatRequest, conversation_id: str | None) -> str | None:
    """会话互斥占位。返回占住的线程 id（释放时要用）；没占返回 None。

    两条放行规则：
    - 审阅动作（resume）走确定性路径、不碰 LLM 与消息历史，秒回——不占位。
    - 其余一律占位：同一会话并发两轮会消息交错、滚动摘要互相覆盖、
      工具轨迹搅在一起（参照 mediago StartRun → 409 的做法）。
    """
    if payload.meta and payload.meta.get("intent") == "resume":
        return None
    thread = conversation_id or chat_svc.GLOBAL_THREAD
    if not chat_svc.acquire_thread(thread):
        raise HTTPException(409, "这个对话正有一轮在运行中，等它结束后再发下一条。")
    return thread


@router.post("/chat")
def chat(payload: ChatRequest) -> dict[str, Any]:
    """对话式 Agent 的一轮非流式对话（同一会话串行，见 _acquire_turn_thread）。"""
    conversation_id = _resolve_conversation(payload.project_id, payload.conversation_id)
    held = _acquire_turn_thread(payload, conversation_id)
    try:
        return chat_svc.chat_once(
            deps.store(), deps.llm(), deps.settings(),
            conversation_id=conversation_id,
            project_id=payload.project_id,
            message=payload.message,
            meta=payload.meta,
            images=payload.images,
            refs=[r.model_dump() for r in (payload.refs or [])],
            runner=deps.subagent_runner(),
        )
    finally:
        if held:
            chat_svc.release_thread(held)


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    """对话式 Agent 的 SSE 流式对话（工具轨迹 + 正文增量 + done）。"""
    conversation_id = _resolve_conversation(payload.project_id, payload.conversation_id)
    held = _acquire_turn_thread(payload, conversation_id)
    handed_off = False
    try:
        gen = chat_svc.chat_stream(
            deps.store(), deps.llm(), deps.settings(),
            conversation_id=conversation_id,
            project_id=payload.project_id,
            message=payload.message,
            meta=payload.meta,
            images=payload.images,
            refs=[r.model_dump() for r in (payload.refs or [])],
            runner=deps.subagent_runner(),
        )

        # chat_stream 是同步生成器（内部含阻塞的 LLM / DB 调用），
        # 必须放到线程池里逐条推进，否则会把整个事件循环卡住，拖死所有并发请求。
        _STREAM_DONE = object()

        async def event_source():
            it = iter(gen)
            try:
                while True:
                    event = await run_in_threadpool(next, it, _STREAM_DONE)
                    if event is _STREAM_DONE:
                        break
                    yield common.sse_frame(event["event"], event["data"])
            finally:
                # 正常耗尽、出错、客户端中途断开（本 generator 被关闭）都会走到这里：
                # 互斥位必须释放，不然这个会话会被一次断开的请求永久锁死。
                if held:
                    chat_svc.release_thread(held)

        response = StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        # 从这里起，释放交棒给 event_source 的 finally；端点自己不再管。
        handed_off = True
        return response
    finally:
        # 构造响应阶段就抛了异常（还没交棒）：就地释放，别把会话锁死。
        if held and not handed_off:
            chat_svc.release_thread(held)


@router.get("/chat/history")
def chat_history(
    project_id: str | None = None, conversation_id: str | None = None
) -> list[dict[str, Any]]:
    """读取某条会话线程的历史消息（优先 conversation_id，否则项目默认对话）。"""
    thread = _resolve_conversation(project_id, conversation_id)
    return chat_svc.load_history(deps.store(), thread)


# ---------- 对话（Conversation）管理：一个项目下多个独立对话 ----------


@router.get("/projects/{project_id}/conversations")
def list_conversations(project_id: str) -> list[dict[str, Any]]:
    """列出某项目下的全部对话（每个对话独立控制上下文）。"""
    common.get_project(project_id)
    st = deps.store()
    result = []
    for c in st.list_conversations(project_id):
        d = _conversation_dict(c)
        d["user_message_count"] = st.count_user_messages(c.id)
        result.append(d)
    return result


@router.post("/projects/{project_id}/conversations")
def create_conversation(project_id: str, payload: ConversationCreate) -> dict[str, Any]:
    """在项目下新建一个对话。"""
    common.get_project(project_id)
    conv = deps.store().create_conversation(project_id, title=payload.title)
    return _conversation_dict(conv)


@router.get("/conversations")
def list_global_conversations() -> list[dict[str, Any]]:
    """列出全局对话（不属于任何项目）。"""
    st = deps.store()
    result = []
    for c in st.list_global_conversations():
        d = _conversation_dict(c)
        d["user_message_count"] = st.count_user_messages(c.id)
        result.append(d)
    return result


@router.post("/conversations")
def create_global_conversation(payload: ConversationCreate) -> dict[str, Any]:
    """新建全局对话（不绑定项目）。"""
    conv = deps.store().create_conversation(project_id=None, title=payload.title)
    return _conversation_dict(conv)


@router.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, payload: ConversationRename) -> dict[str, Any]:
    conv = deps.store().rename_conversation(conversation_id, payload.title)
    if not conv:
        raise HTTPException(404, "对话不存在")
    return _conversation_dict(conv)


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str) -> dict[str, Any]:
    if not deps.store().delete_conversation(conversation_id):
        raise HTTPException(404, "对话不存在")
    return {"deleted": conversation_id}


@router.get("/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: str) -> list[dict[str, Any]]:
    """读取某个对话的全部消息（按时间正序）。"""
    conv = deps.store().get_conversation(conversation_id)
    if not conv:
        raise HTTPException(404, "对话不存在")
    return chat_svc.load_history(deps.store(), conv.id)
