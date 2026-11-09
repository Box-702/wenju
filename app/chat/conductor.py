# =====================================================================
# conductor.py —— 对话编排层（Codex / DSH 风格的工作形式）
#
# 职责只剩「编排」，不装任何工具实现：
#   - 系统提示（触发器层）：决定该调哪个工具；执行细则外置成
#     app/skills/*（audience: main），由 load_skill 工具按需拉取；
#   - 对话内核：app/chat/loop.py 的显式工具循环（不再用 LangGraph 图）；
#   - 轮次服务：历史压缩、事件归集、编造拦截、SSE 流式输出；
#   - 审阅动作（接受 / 拒绝 / 重新生成）走确定性路径，不经过 LLM。
#
# 工具实现在 ./tools/ 包（screenplay / crew / video / memory_tools 四个域）。
#
# 服务入口：
#   chat_once()    一次非流式对话（返回完整回复）；
#   chat_stream()  SSE 流式对话（先推工具轨迹，再流式输出正文）；
#   _handle_resume() 审阅动作走确定性路径。
# =====================================================================

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from .. import memory
from ..agent.tasks import SubAgentRunner
from ..config import Settings
from ..llm import LLM
from ..skills import skill_index
from ..store import Store
from .loop import AgentLoop, assistant_message, system_message, user_message
from .tools import build_chat_tools as _assemble_tools
from .tools.context import ADAPT_TYPE_LABELS as _ADAPT_TYPE_LABELS
from .tools.context import Collector
from .tools.context import short as _short

log = logging.getLogger(__name__)

# 对话 conductor 的工具循环上限（定义在内核里，这里只做再导出）。
from .loop import MAX_TOOL_STEPS as MAX_CHAT_TOOL_STEPS  # noqa: E402

# 重建历史上下文时携带的图片上限（按时间取最近的）。多模态图片以 base64
# 进请求体，一张图就是几千 token——超出部分的旧图降级成文字占位，防止
# 长图片对话把上下文撑爆。最近窗口内的图片始终保留，模型不会「忘图」。
MAX_CONTEXT_IMAGES = 12

# 对话线程：项目下每个对话（Conversation）一条独立线程；
# 还没有项目的全局对话用固定值 GLOBAL_THREAD。
GLOBAL_THREAD = "global"

# 兼容别名：工具闭包写入的收集器类型现在定义在 tools.context。
_Collector = Collector

# 系统提示分两层：
#   · 触发器（每一轮都要用）留在提示词里——它决定「该调哪个工具」，是路由信息；
#   · 执行细则（步骤顺序、硬门槛、失败怎么处理）外置成 app/skills/*（audience: main），
#     由 load_skill 工具按需拉取。
# 为什么不把触发器也搬进技能：路由每轮都要用，搬出去等于每轮多一次工具往返，
# 模型一旦懒得加载就等于不会做事。技能只负责「进去之后怎么做」。
SYSTEM_PROMPT_TEMPLATE = (
    "你是「问剧」的改编协作者 Agent，工作形式类似 Codex / DSH：通过对话与作者协作，"
    "把一部作品改编成结构化剧本，并持续打磨到成片。\n"
    "所有动作都通过工具完成，不要凭空叙述已经做过的事。\n"
    "\n"
    "【触发 → 动作】\n"
    "· 新建作品（新建项目 / 新建剧本 / 新开一部）→ create_project；缺标题、改编类型或原文时先问清楚\n"
    "· 生成初稿（生成 / 初稿 / 第一版 / 出剧本）→ generate_script\n"
    "· 改编（改对白 / 改节奏 / 压缩 / 反转 / 口语化）→ run_adaptation，细则见 script-review 技能\n"
    "· 咨询（剧本怎么样 / 这个结尾好不好）→ ask，凭剧本概况与作者风格给有依据的回答\n"
    "· 读原文（原文第几段写了什么 / 原文里怎么写的某句话 / 核对初稿有没有偏离原文）"
    "→ read_source 分段读、search_source 定位关键词；原文很长时先用 search_source 定位再读\n"
    "· 拆镜头（拆解 / 分镜 / 设计镜头 / 场景拆解）→ breakdown_scenes\n"
    "· 风格指南（视觉风格 / 角色造型 / 画面风格）→ generate_style_guide\n"
    "· 定妆图（参考图 / 人物形象图 / 视觉参考）→ generate_reference_assets\n"
    "· 视频 prompt（生成 prompt / 镜头描述）→ generate_video_prompts\n"
    "· 开始制作（一键制作 / 跑完整流程 / 出一版提示词）→ start_production，"
    "五阶段（剧本→拆解→风格→资产图→提示词）自动跑完，已完成的阶段自动复用\n"
    "· 问进度（到哪了 / 拆好了吗 / 出片了吗 / 合成好了没 / 哪些还没批准）→ get_status，"
    "一次问清，不要凭记忆答，也不要靠猜\n"
    "· 批准提示词 → approve_video_prompts（只在用户明确说「批准」时）\n"
    "· 出片（出片 / 生成视频 / 提交视频）→ submit_videos；重试或重 roll 某镜 → rerun_video\n"
    "· 合成成片（拼成片 / 出完整视频）→ finalize_film\n"
    "· 记忆：系统提示里的「用户记忆」要主动遵循；说「记住… / 以后都…」→ remember；问「你记得什么」→ recall\n"
    "· 长篇小说（原文超过约 1.4 万字）→ 先加载 novel-serial 技能，不要直接 generate_script\n"
    "\n"
    "【可用技能】触发器只告诉你该做哪件事，怎么做的细则在技能里，\n"
    "   · 动手之前先 load_skill：只要本轮要执行某个领域的动作（生成 / 改编 / 拆解 / 提交 / 批准 / 合成），"
    "本轮第一个工具调用就应该是 load_skill，拿完细则再动手；\n"
    "   · 例外：纯只读的盘点（看镜头进度、看哪些提示词待批、查后台任务）可以先查，但一旦要动手，先补上 load_skill；\n"
    "   · 只加载与本轮任务相关的那个技能，不要为求保险全加载一遍；同一轮加载过的不必重复。\n"
    "{skill_index}\n"
    "\n"
    "【硬规矩】\n"
    "0. 用户用 @ 引用资产（@林然 / @滨江路）：被 @ 到的就是本轮要处理的对象——"
    "回答里直接用这个称呼，改它要走资产相关工具（改写定妆图 / 登记素材 / 重新生成该角色资产），"
    "不要凭空描述、也不要换成别的角色；本轮上下文里的「本轮引用的资产」列的就是它们。\n"
    "1. 汇报以工具返回为准：说「已生成 / 已建成 / 已挑出 / 已提交 / 已出片」之前，本轮必须真的调用过对应工具；"
    "严禁凭对话历史编造版本号（ver_xxx）、场次、进度或任务结果。没调工具就直说还没做，或者先做再说。\n"
    "2. 不替用户做最终决定：接受、拒绝、批准永远由用户发起，你只负责解释影响和引导。\n"
    "3. 剧组任务（拆解 / 风格 / 定妆图 / 提示词）与出片都在后台运行，启动后告知用户可在右栏查看进度。\n"
    "4. 回复简洁、口语化、用简体中文；不确定时先问；不要虚构剧本内容。\n"
    "5. 出片走 MiniMax，需要用户先配置自己的 API Key。\n"
)


def system_prompt() -> str:
    """组装系统提示：静态部分 + 主 Agent 技能目录（渐进披露第一级）。

    目录只有 name + description + when_to_use，正文要靠 load_skill 拉——
    所以技能越多，提示词也只长几行。
    """
    # 用 replace 而不是 str.format：技能描述里可能出现花括号，format 会直接炸。
    return SYSTEM_PROMPT_TEMPLATE.replace("{skill_index}", skill_index("main"))


def main_skill_names() -> set[str]:
    """主 Agent 能加载的技能名（供工具校验与测试断言，避免两处硬编码）。"""
    from .tools.context import main_skill_names as _names

    return _names()


# ---------- 工具集 ----------


def build_chat_tools(
    store: Store,
    llm: LLM,
    collector: _Collector,
    runner: SubAgentRunner | None = None,
    project_id: str | None = None,
) -> list[Any]:
    """构造对话 conductor 的工具集（实现在 ./tools/ 按域拆分）。

    project_id：本轮对话所属的作品——工具漏传 project_id 时的兜底。
    """
    return _assemble_tools(store, llm, collector, runner=runner, project_id=project_id)


# ---------- 对话内核 ----------


def build_agent_loop(
    store: Store,
    llm: LLM,
    collector: _Collector,
    runner: SubAgentRunner | None = None,
    *,
    has_images: bool = False,
    project_id: str | None = None,
) -> AgentLoop:
    """构建（单轮）对话内核：显式工具循环。

    has_images：本轮用户消息带图——循环换用多模态模型解析
    （llm.chat_multimodal()：不支持视觉的对话厂商自动换到同厂视觉型号）。
    project_id：本轮对话所属的作品——工具漏传 project_id 时的兜底。
    """
    tools = build_chat_tools(store, llm, collector, runner=runner, project_id=project_id)
    return AgentLoop(llm, tools, has_images=has_images, current_project_id=project_id)


# ---------- 历史 ----------


def load_history(store: Store, conversation_id: str | None) -> list[dict[str, Any]]:
    """读取某条会话线程（= Conversation id）的历史消息（用于前端首屏渲染）。"""
    thread = conversation_id or GLOBAL_THREAD
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            # 字段名与前端契约保持一致：payloads（复数）。此前误作 payload（单数），
            # 历史消息里的审阅卡片载荷从未被前端读到。
            "payloads": m.payload,
            "events": m.events,
            "images": m.images,
            "created_at": m.created_at.isoformat(),
        }
        for m in store.list_chat_messages(thread)
    ]


# ---------- 图片（多模态上下文） ----------


def _resolve_image_refs(store: Store, image_ids: list[str] | None) -> list[dict[str, Any]]:
    """把前端传来的图片 id 解析成落库快照 [{id,url,mime,name}]；无效 id 丢弃。"""
    result: list[dict[str, Any]] = []
    for image_id in image_ids or []:
        img = store.get_chat_image(str(image_id))
        if img is None:
            continue
        result.append({
            "id": img.id,
            "url": f"/api/chat/images/{img.id}",
            "mime": img.mime,
            "name": img.name,
        })
    return result


def _image_data_url(store: Store, image_id: str) -> str | None:
    """读取入库图片字节，转成 base64 data URL（多模态模型的标准喂法）。"""
    img = store.get_chat_image(image_id)
    if img is None:
        return None
    b64 = base64.b64encode(img.content).decode("ascii")
    return f"data:{img.mime};base64,{b64}"


def _history_messages(
    store: Store,
    conversation_id: str | None,
    messages: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """把对话历史转成 OpenAI 兼容的 dict 消息（user/assistant），供本轮模型参考。

    传 messages（memory.compress_history_if_needed 的最近窗口）用之；否则直接读库。
    assistant 消息会附上压缩的工具轨迹（来自落库 events）：模型对「我上一轮做了
    什么」的记忆不再只靠自己当时的总结，多轮协作不会丢事实。

    图片常驻上下文：user 消息带的图片按落库快照重建为 image_url 内容块，
    重新喂给多模态模型——图片不会「聊两句就忘」。仅最近 MAX_CONTEXT_IMAGES
    张图保留原图，更早的降级为文字占位（防止请求体无限膨胀）。
    """
    if messages is None:
        messages = store.list_chat_messages(conversation_id or GLOBAL_THREAD, limit=60)

    # 先盘点全部历史图片，决定哪些保留原图、哪些降级。
    all_image_ids: list[str] = []
    for m in messages:
        if m.role == "user":
            for ref in (m.images or []):
                if ref.get("id"):
                    all_image_ids.append(str(ref["id"]))
    keep_ids = set(all_image_ids[-MAX_CONTEXT_IMAGES:])

    result: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            image_refs = [ref for ref in (m.images or []) if ref.get("id")]
            if image_refs:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for ref in image_refs:
                    image_id = str(ref["id"])
                    url = _image_data_url(store, image_id) if image_id in keep_ids else None
                    if url:
                        blocks.append({"type": "image_url", "image_url": {"url": url}})
                    else:
                        blocks.append({"type": "text", "text": "[此前上传的图片（已省略原图）]"})
                result.append(user_message(blocks))
            else:
                result.append(user_message(m.content))
        elif m.role == "assistant" and m.content:
            content = m.content
            traj = _trajectory_note(m.events or [])
            if traj:
                content = f"{traj}\n\n{content}"
            result.append(assistant_message(content))
    return result


_TOOL_ACTION_LABELS = {
    "create_project": "新建项目", "generate_script": "生成初稿", "run_adaptation": "发起改编",
    "ask": "查阅剧本内容",
    "breakdown_scenes": "拆解镜头", "generate_style_guide": "生成视觉风格",
    "generate_video_prompts": "撰写镜头提示词", "generate_reference_assets": "生成定妆图",
    "get_status": "查询进度", "approve_video_prompts": "批准提示词",
    "submit_videos": "提交出片",
    "rerun_video": "重roll镜头", "add_asset": "登记素材", "finalize_film": "合成成片",
    "map_novel": "建立章节地图", "propose_arcs": "挑选改编段落",
}


def _trajectory_note(events: list[dict[str, Any]]) -> str:
    """把一轮的工具轨迹压缩成一行事实记录（仅进 LLM 上下文，不展示给用户）。"""
    result_names = {ev.get("name") for ev in events if ev.get("type") == "tool_result"}
    parts: list[str] = []
    for ev in events:
        name = ev.get("name") or ""
        if not name:
            continue
        label = _TOOL_ACTION_LABELS.get(name, name)
        if ev.get("type") == "tool_result":
            summary = _short(ev.get("summary") or "", 80)
            parts.append(f"{label}：{summary}" if summary else label)
        elif ev.get("type") == "tool_call" and name not in result_names:
            parts.append(f"{label}（未返回）")
    if not parts:
        return ""
    deduped: list[str] = []
    for part in parts:
        if part not in deduped:
            deduped.append(part)
    return "［本轮动作］" + "；".join(deduped[:8])


# ---------- 对话服务 ----------


def _prepare_turn(
    store: Store, llm: LLM, runner: SubAgentRunner | None, conversation_id: str | None,
    project_id: str | None, message: str, meta: dict[str, Any] | None,
    images: list[dict[str, Any]] | None = None,
    refs: list[dict[str, Any]] | None = None,
) -> tuple[_Collector, AgentLoop, list[dict[str, Any]]]:
    """准备一轮对话的 collector, 内核, messages。

    上下文组成：系统提示 + 项目概况 + 用户记忆 + [历史滚动摘要] + 最近消息 + 本轮输入。
    长对话由 memory.compress_history_if_needed 压缩旧消息，掐头不再丢事实。
    本轮带图时用户消息构造为多模态内容块（text + image_url）。

    消息是 OpenAI 兼容的 dict（见 chat/loop.py），不再用 LangChain 消息类型。
    """
    collector: _Collector = {"project_id": project_id, "payloads": []}
    thread = conversation_id or GLOBAL_THREAD
    collector["conversation_id"] = thread if conversation_id else None
    loop = build_agent_loop(
        store, llm, collector, runner=runner,
        has_images=bool(images), project_id=project_id,
    )
    note = _current_project_note(store, project_id)
    memory_note = memory.format_memory_note(store, project_id=project_id, conversation_id=thread)
    summary, recent = memory.compress_history_if_needed(store, llm, thread)
    history = _history_messages(store, thread, messages=recent)
    if summary:
        history = [system_message(f"[此前对话摘要]\n{summary}")] + history
    messages: list[dict[str, Any]] = [
        system_message(system_prompt() + "\n" + note + memory_note),
        *history,
        user_message(_user_turn_content(message, images, refs)),
    ]
    return collector, loop, messages


_REF_KIND_ZH = {"character": "角色", "location": "场景", "asset": "素材"}


def _refs_note(refs: list[dict[str, Any]] | None) -> str:
    """把用户用 @ 引用的资产列成一段，附在本轮消息后面。

    为什么要有这一段：输入框里只有「@林然」三个字，Agent 不知道它是哪部作品里的
    哪个角色（同名角色、没定妆图的角色都存在）。列清楚「类型 + 编号 + 名字」，
    Agent 才能定位到资产库条目去改。
    """
    items = [r for r in (refs or []) if (r.get("name") or r.get("id"))]
    if not items:
        return ""
    lines = ["【本轮引用的资产】"]
    for r in items:
        kind = _REF_KIND_ZH.get(str(r.get("kind") or ""), "资产")
        ident = str(r.get("id") or "").strip()
        name = str(r.get("name") or "").strip()
        lines.append(f"- @{name}（{kind} {ident}）".replace("  ", " "))
    return "\n".join(lines)


def _user_turn_content(
    message: str,
    images: list[dict[str, Any]] | None,
    refs: list[dict[str, Any]] | None = None,
) -> Any:
    """构造本轮用户消息：带图时为多模态内容块，否则纯文本。"""
    note = _refs_note(refs)
    if note:
        message = f"{message}\n\n{note}" if message else note
    if not images:
        return message
    blocks: list[dict[str, Any]] = []
    if message:
        blocks.append({"type": "text", "text": message})
    for ref in images:
        url = ref.get("data_url")
        if url:
            blocks.append({"type": "image_url", "image_url": {"url": url}})
    if not blocks:
        return message
    return blocks


def _turn_meta(llm: LLM, started: float) -> dict[str, Any]:
    """这一轮的元信息：模型名 + 耗时（毫秒）。

    放在消息头（模型徽标 · 处理 Xs），是「对话产品」的基本信息量——
    用户要知道这一轮是谁答的、答了多久。
    """
    info = llm.describe() if llm.available else {}
    return {
        "model": str(info.get("model") or info.get("provider_label") or ""),
        "provider": str(info.get("provider_label") or info.get("provider") or ""),
        "ms": int((time.monotonic() - started) * 1000),
    }


def _finalize_turn(
    store: Store, collector: _Collector, conversation_id: str | None,
    project_id: str | None, message: str, meta: dict[str, Any] | None,
    final_text: str, events: list[dict[str, Any]],
    save_user: bool = True,
    images: list[dict[str, Any]] | None = None,
    turn_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存消息并返回对话结果。

    save_user=False：流式路径已在流开始时保存过用户消息，这里只补 assistant，
    避免同一条用户消息重复入库。
    images：本轮用户消息的图片快照（[{id,url,mime,name}]），随消息落库，
    之后每轮重建上下文都会重新取回原图字节。
    """
    final_text = _guard_unverified_claims(final_text, events)
    thread = conversation_id or GLOBAL_THREAD
    new_project_id = collector.get("project_id") or project_id
    if save_user:
        store.save_chat_message(thread_id=thread, role="user", content=message,
                                payload=[meta or {}], images=images)
    # 消息元信息（模型 · 耗时）放在 events 首位——它是「这一轮」的元信息，
    # 不是工具轨迹的一部分；前端渲染成消息头，步骤列表会把它过滤掉。
    all_events = events
    if turn_meta:
        all_events = [{"type": "meta", **turn_meta}, *events]
    store.save_chat_message(
        thread_id=thread, role="assistant", content=final_text,
        payload=collector.get("payloads") or [], events=all_events,
    )
    return {
        "reply": final_text,
        "payloads": collector.get("payloads") or [],
        "events": events,
        # turn_meta（模型 · 耗时）单独带出去：流式路径里前端已经在收事件，
        # 这一条用于给「正在流式的那条消息」补上消息头，不用等历史回放。
        "turn_meta": turn_meta,
        "project_id": new_project_id,
        "thread_id": thread,
    }


# 声称「已经完成某动作」的结果短语。模型（deepseek 实测）偶尔会模仿历史消息里的
# 「［本轮动作］生成初稿：已生成初稿版本 ver_xxx…」轨迹格式，在没调工具的情况下
# 凭对话记忆编一份结果（假版本号、假进度）。这里做确定性护栏：有结果短语、
# 本轮却没有一条真实的工具返回 → 追加系统更正，防止用户被假版本号误导。
_CLAIM_PATTERNS = (
    "已生成初稿",
    "已改编自",
    "已改编到第",
    "章节地图已建成",
    "个候选故事弧",
    "已提交出片",
    "已批准",
)

_FABRICATED_NOTICE = (
    "\n\n（系统更正：本轮我没有实际调用工具，上面提到的「结果」是不可信的误报。"
    "请再说一次你的需求，我会真正执行、以工具返回为准后再汇报。）"
)

_CORRECTION_PROMPT = (
    "（系统拦截：你上一条回复在没有调用任何工具的情况下报告了「结果」——这是编造，已被系统标记。"
    "请现在真正调用需要的工具完成用户请求；确实做不到就如实说明原因。一切以工具返回为准。）"
)


def _execute_loop_turn(loop: AgentLoop, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """跑一轮对话内核（非流式），返回 (最终回复文本, 工具事件)。"""
    result = loop.run(messages)
    return result.text, result.events


def _claims_unverified(final_text: str, events: list[dict[str, Any]] | None) -> bool:
    """回复里有没有「声称了动作结果、但本轮没有任何真实工具返回」。"""
    text = final_text or ""
    if not text or any(ev.get("type") == "tool_result" for ev in events or []):
        return False
    return any(pattern in text for pattern in _CLAIM_PATTERNS)


def _guard_unverified_claims(final_text: str, events: list[dict[str, Any]]) -> str:
    """回复声称了动作结果、但本轮没有任何真实工具返回时，追加系统更正。"""
    text = final_text or ""
    if _claims_unverified(text, events):
        return text + _FABRICATED_NOTICE
    return text


def _current_project_note(store: Store, project_id: str | None) -> str:
    if not project_id:
        return "当前还没有选定作品。如果用户要新建作品，调用 create_project；创建后把项目 id 交给用户，并提示已创建。"
    p = store.get_project(project_id)
    if p is None:
        return f"注意：project_id={project_id} 在数据库中不存在，需要时请先创建项目。"
    version = store.latest_version(p)
    version_note = "（尚未生成初稿）" if version is None else f"（当前版本 {version.id}，共 {len(version.script.scenes)} 场）"
    return f"当前作品：《{p.title}》 id={p.id}，类型 {_ADAPT_TYPE_LABELS.get(p.adaptation_type, p.adaptation_type)}{version_note}。"


def _humanized_user_message(meta: dict[str, Any] | None, message: str) -> str:
    """把审阅动作转成可读的用户消息（用于历史展示）。"""
    if not meta or meta.get("intent") != "resume":
        return message
    action = meta.get("action", "accept")
    if action == "accept":
        indexes = meta.get("patch_indexes")
        if indexes:
            return f"接受改编提议（勾选 {len(indexes)} 项）"
        return "接受全部改编提议"
    if action == "reject":
        return "拒绝这次改编提议"
    if action == "regenerate":
        return f"重新生成（反馈：{meta.get('feedback') or '换个思路'}）"
    if action == "edit":
        return "编辑 patch 后接受"
    return message


def _resume_reply(store: Store, result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """把 resume 结果转成 (助手正文, 结构化载荷)。"""
    status = result.get("status")
    if status == "reviewing":
        payload = {
            "type": "patch_review",
            "run_id": result.get("run_id"),
            "plan": result.get("plan") or [],
            "patch": result.get("patch") or [],
            "steps": result.get("steps") or [],
            "status": "reviewing",
            "review": result.get("review"),
        }
        return "已按你的反馈重新生成提议，请继续审阅下面的卡片。", [payload]
    if status == "applied":
        vid = result.get("new_version_id")
        issues = result.get("validation_issues") or []
        errors = [i for i in issues if i.get("severity") == "error"]
        payload = {
            "type": "version_applied",
            "version_id": vid,
            "fallback": bool(result.get("fallback")),
            "validation_issues": issues,
        }
        note = "（兜底应用：线程状态丢失，基于已落库提议直接应用）" if result.get("fallback") else ""
        return f"✅ 已接受，生成新版本 {vid}，校验问题 {len(errors)} 项。{note}", [payload]
    if status == "rejected":
        return "已拒绝这次改编提议，剧本保持不变。", []
    if status == "failed":
        return f"⚠️ 应用改动失败，未生成新版本：{result.get('error') or '校验未通过'}。", []
    if status == "not_found":
        return "运行记录不存在，可能已过期。", []
    return f"操作完成（状态：{status}）。", []


def _handle_resume(
    store: Store,
    llm: LLM,
    settings: Settings,
    *,
    run_id: str,
    action: str,
    patch_indexes: list[int] | None,
    feedback: str | None,
    patch: list[dict[str, Any]] | None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """确定性处理审阅动作（不经过 LLM），复用底层 resume_agent_run。"""
    from ..agent import runner as agent_svc

    run = store.get_agent_run(run_id)
    if run is None:
        return {"reply": "运行记录不存在，可能已过期。", "payloads": [], "result": {"status": "not_found"}}
    if run.status in ("applied", "rejected"):
        return {
            "reply": f"这条提议已处理过（状态：{run.status}），无需重复操作。",
            "payloads": [],
            "result": {"status": run.status},
        }
    result = agent_svc.resume_agent_run(
        store,
        llm,
        settings,
        run_id=run_id,
        action=action,
        patch_indexes=patch_indexes,
        patch=patch,
        feedback=feedback,
    )
    # 审阅动作沉淀为记忆：接受 → 项目决定；拒绝/重生成带反馈 → 对话反馈。
    memory.learn_from_decision(
        store,
        project_id=run.project_id,
        conversation_id=conversation_id,
        action=action,
        instruction=run.user_prompt,
        feedback=feedback,
        patch_count=len(run.patch) if action == "accept" else 0,
    )
    reply, payloads = _resume_reply(store, result)
    return {"reply": reply, "payloads": payloads, "result": result}


def _run_turn(
    store: Store,
    llm: LLM,
    settings: Settings,
    *,
    conversation_id: str | None,
    project_id: str | None,
    message: str,
    meta: dict[str, Any] | None,
    runner: SubAgentRunner | None = None,
    images: list[str] | None = None,
    refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行一轮对话（非流式），返回 {reply, payloads, events, project_id, thread_id}。"""
    # 审阅动作走确定性路径。
    if meta and meta.get("intent") == "resume":
        handled = _handle_resume(
            store, llm, settings,
            run_id=str(meta.get("run_id") or ""),
            action=str(meta.get("action") or "accept"),
            patch_indexes=meta.get("patch_indexes"),
            feedback=meta.get("feedback"),
            patch=meta.get("patch"),
            conversation_id=conversation_id,
        )
        thread = conversation_id or GLOBAL_THREAD
        store.save_chat_message(thread_id=thread, role="user", content=_humanized_user_message(meta, message), payload=[meta])
        store.save_chat_message(thread_id=thread, role="assistant", content=handled["reply"], payload=handled["payloads"], events=[])
        return {
            "reply": handled["reply"],
            "payloads": handled["payloads"],
            "events": [],
            "project_id": project_id,
            "thread_id": thread,
        }

    image_refs = _resolve_image_refs(store, images)
    turn_images = [
        {**ref, "data_url": _image_data_url(store, ref["id"])}
        for ref in image_refs
    ]
    collector, loop, messages = _prepare_turn(
        store, llm, runner,
        conversation_id, project_id, message, meta, images=turn_images, refs=refs,
    )
    events: list[dict[str, Any]] = []
    final_text = ""
    started = time.monotonic()
    try:
        final_text, events = _execute_loop_turn(loop, messages)
        if _claims_unverified(final_text, events):
            # 编造拦截：模型没调工具就报告了「结果」。带着更正指令重跑一次，
            # 让请求真正落到工具上；重跑仍失败则如实把更正留给 finalize。
            log.warning("对话轮检测到未执行的动作报告，自动重跑一次")
            retry_messages = list(messages) + [user_message(_CORRECTION_PROMPT)]
            retry_text, retry_events = _execute_loop_turn(loop, retry_messages)
            if retry_text:
                final_text, events = retry_text, events + retry_events
    except Exception as e:  # noqa: BLE001
        log.warning("对话运行失败：%s", e)
        final_text = f"对话运行出错了：{e}"

    return _finalize_turn(store, collector, conversation_id, project_id, message, meta, final_text, events,
                          images=image_refs, turn_meta=_turn_meta(llm, started))


def chat_once(
    store: Store,
    llm: LLM,
    settings: Settings,
    *,
    conversation_id: str | None,
    project_id: str | None,
    message: str,
    meta: dict[str, Any] | None = None,
    runner: SubAgentRunner | None = None,
    images: list[str] | None = None,
    refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """非流式对话：执行一轮并返回完整结果。"""
    return _run_turn(
        store, llm, settings,
        conversation_id=conversation_id, project_id=project_id, message=message, meta=meta,
        runner=runner, images=images, refs=refs,
    )


def chat_stream(
    store: Store,
    llm: LLM,
    settings: Settings,
    *,
    conversation_id: str | None,
    project_id: str | None,
    message: str,
    meta: dict[str, Any] | None = None,
    runner: SubAgentRunner | None = None,
    images: list[str] | None = None,
    refs: list[dict[str, Any]] | None = None,
):
    """SSE 流式对话：逐条 yield {event, data} 事件。

    事件序列：tool_call / tool_result（工具轨迹）-> token（正文增量）
    -> done（完整结果）。审阅动作直接返回 done（无 LLM）。
    """
    # 审阅动作：确定性执行，直接产出 done。
    if meta and meta.get("intent") == "resume":
        handled = _handle_resume(
            store, llm, settings,
            run_id=str(meta.get("run_id") or ""),
            action=str(meta.get("action") or "accept"),
            patch_indexes=meta.get("patch_indexes"),
            feedback=meta.get("feedback"),
            patch=meta.get("patch"),
            conversation_id=conversation_id,
        )
        thread = conversation_id or GLOBAL_THREAD
        store.save_chat_message(thread_id=thread, role="user", content=_humanized_user_message(meta, message), payload=[meta])
        store.save_chat_message(thread_id=thread, role="assistant", content=handled["reply"], payload=handled["payloads"], events=[])
        yield {"event": "done", "data": {
            "reply": handled["reply"],
            "payloads": handled["payloads"],
            "events": [],
            "project_id": project_id,
            "thread_id": thread,
        }}
        return

    image_refs = _resolve_image_refs(store, images)
    turn_images = [
        {**ref, "data_url": _image_data_url(store, ref["id"])}
        for ref in image_refs
    ]
    collector, loop, messages = _prepare_turn(
        store, llm, runner,
        conversation_id, project_id, message, meta, images=turn_images, refs=refs,
    )
    # 用户消息在流开始时就落库：生成中途断开（客户端切走/刷新）或出错时、
    # 至少保住用户说过的话——此前只在整轮结束时统一保存，一旦中断整段丢失。
    # 图片快照一并落库（历史回放与后续轮次的上下文重建都靠它）。
    thread = conversation_id or GLOBAL_THREAD
    store.save_chat_message(thread_id=thread, role="user", content=message, payload=[meta or {}], images=image_refs)
    events: list[dict[str, Any]] = []
    final_text = ""
    started = time.monotonic()

    def _pump(msgs: list[dict[str, Any]]):
        """跑一遍内核并实时 yield 事件；正文增量累积到 holder。"""
        nonlocal final_text
        text_parts: list[str] = []
        last_complete = ""
        for ev in loop.stream(msgs):
            if ev.type == "_complete":
                last_complete = str(ev.data.get("text") or "")
                continue
            if ev.type == "_truncated":
                continue
            if ev.type == "token":
                text_parts.append(str(ev.data.get("delta") or ""))
            elif ev.type in ("tool_call", "tool_result"):
                events.append({"type": ev.type, **ev.data})
            yield {"event": ev.type, "data": ev.data}
        final_text = "".join(text_parts) or last_complete

    try:
        for sse in _pump(messages):
            yield sse
        if _claims_unverified(final_text, events):
            # 编造拦截：流式路径同样自动重跑一次（done 事件会用重跑结果覆盖正文）。
            log.warning("对话流式轮检测到未执行的动作报告，自动重跑一次")
            for sse in _pump(list(messages) + [user_message(_CORRECTION_PROMPT)]):
                yield sse
    except GeneratorExit:
        # 客户端断开（切走会话/刷新页面）：把已生成的部分回复落库，避免全部丢失。
        if final_text:
            store.save_chat_message(
                thread_id=thread, role="assistant", content=final_text,
                payload=collector.get("payloads") or [], events=events,
            )
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("对话流式运行失败：%s", e)
        yield {"event": "error", "data": {"message": str(e)}}
        return

    result = _finalize_turn(
        store, collector, conversation_id, project_id, message, meta, final_text, events,
        save_user=False, turn_meta=_turn_meta(llm, started),
    )
    yield {"event": "done", "data": result}
