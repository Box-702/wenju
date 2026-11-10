# =====================================================================
# projects.py —— 项目与本地文件
#
# 项目生命周期（新建 / 导入 / 列表 / 删除）、初稿生成、工作目录落盘、
# 编剧圣经笔记、题材知识查看、磁盘文件浏览。
# =====================================================================

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from ..domain import normalize_adaptation_type, normalize_visual_style
from ..pipeline.generation import generate_script
from ..pipeline.importer import parse_file
from ..pipeline.knowledge import detect_genres, extract_author_style
from ..pipeline.patch import validate_script
from . import common, deps
from .schemas import GenerateRequest, NotesSet, ProjectCreate

router = APIRouter()


#: 模型失败的原因 → 人话。返回空串表示「就是没配模型」。
def _fallback_reason(artifacts: dict[str, Any]) -> str:
    """把回退时的异常翻译成用户能照做的提示。

    「未配置可用模型，配置 key 可生成真实剧本」这句话在**已经配了 key** 的场景下是错的：
    余额耗尽（402）、key 无效（401）、限流（429）都会走到同一个 local-fallback，
    此时让用户去「配置 key」等于让他去修一个没坏的东西。
    """
    err = str(artifacts.get("error") or "")
    low = err.lower()
    if "insufficient" in low or "402" in low or "余额" in err:
        return "模型余额不足"
    if "401" in low or "unauthorized" in low or "invalid api key" in low or "invalid_api_key" in low:
        return "模型 key 无效或无权限"
    if "429" in low or "rate limit" in low:
        return "模型限流"
    if "timeout" in low or "timed out" in low:
        return "模型调用超时"
    return ""


# ---------- 项目 ----------


@router.post("/projects")
def create_project(payload: ProjectCreate) -> dict[str, Any]:
    p = deps.store().create_project(
        title=payload.title,
        adaptation_type=payload.adaptation_type,
        language=payload.language,
        raw_text=payload.raw_text,
    )
    common.persist_original(p)
    # 作者风格只做规则画像（无网络调用）：生成链路本就会用规则版重算
    try:
        style = extract_author_style(payload.raw_text)
        deps.store().set_project_notes(p.id, f"作者风格：{style.get('summary', '')}")
    except Exception:
        pass
    genres = detect_genres(payload.raw_text, top=2)
    return {"id": p.id, "genres": genres}


@router.get("/projects")
def list_projects() -> list[dict[str, Any]]:
    return deps.store().list_project_summaries()


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    p = common.get_project(project_id)
    data = common.project_dict(p)
    data["novel_map"] = p.novel_map  # 完整章节地图（列表接口只给章节数）
    return data


@router.get("/projects/{project_id}/overview")
def project_overview(project_id: str) -> dict[str, Any]:
    """作品的「消耗 / 产出」概览：场/镜/成片计数、定妆图与音色登记、出片预估费用。

    只聚合已落库的数据（C7 最小版，见 docs/PLAN-mediago-reference.md），
    不新增任何采集点。前端进右栏/作品页的一张小卡，说创作者语言。
    """
    p = common.get_project(project_id)
    return common.project_overview(p)


@router.delete("/projects/{project_id}")
def delete_project(project_id: str) -> dict[str, Any]:
    """删除项目及其所有对话、消息、版本，并清理磁盘上的工作目录文件夹。"""
    p = common.get_project(project_id)
    title = p.title
    # 磁盘目录以 title 为键：若有同名项目共存，删掉其中一个不能 rmtree 共享目录。
    has_same_title = any(
        other.title == title and other.id != project_id for other in deps.store().list_projects()
    )
    deps.store().delete_project(project_id)
    ws = common.workspace()
    if ws.configured and not has_same_title:
        try:
            ws.remove_project(title)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "deleted": project_id}


@router.post("/projects/{project_id}/generate")
def generate(project_id: str, payload: GenerateRequest) -> dict[str, Any]:
    p = common.get_project(project_id)
    adaptation_type = payload.adaptation_type or p.adaptation_type
    language = payload.language or p.language
    script, artifacts = generate_script(
        deps.llm(), deps.settings(),
        title=p.title, raw_text=p.raw_text, adaptation_type=adaptation_type, language=language,
    )
    version = deps.store().create_version(
        p,
        script,
        source_type="generation",
        label="初始生成",
        notes=f"生成模式：{artifacts.get('mode', 'n/a')}",
        parent_version_id=p.current_version_id,
        set_current=True,
    )
    common.persist_version(p, version)
    warnings = []
    if artifacts.get("mode") == "local-fallback":
        reason = _fallback_reason(artifacts)
        if reason:
            warnings.append(
                f"{reason}，本次落库的是本地规则剧本（不是模型产出）。"
                "充值或换一个可用的 key 后重新生成即可。"
            )
        else:
            warnings.append(
                "未配置可用模型，本次生成的是本地演示剧本；配置 OPENAI/DEEPSEEK key 可生成真实剧本。"
            )
    return {
        "version_id": version.id,
        "mode": artifacts.get("mode"),
        "validation": [i.model_dump() for i in validate_script(script)],
        "warnings": warnings,
    }


# ---------- 文件导入：新建剧本 = 上传 .txt / .md / .docx ----------


@router.post("/projects/import")
async def import_project(
    file: UploadFile | None = File(default=None),  # noqa: B008
    raw_text: str | None = Form(default=None),
    title: str | None = Form(default=None),
    adaptation_type: str = Form(default="short_drama"),
    visual_style: str = Form(default="realistic"),
    language: str = Form(default="zh-CN"),
) -> dict[str, Any]:
    """新建剧本：上传原著文件（.txt/.md/.docx）或直接粘贴原文，自动建知识库。"""
    text = ""
    source_file = ""
    if file is not None and file.filename:
        data = await file.read()
        if not data:
            raise HTTPException(400, "文件为空")
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(400, "文件太大（上限 10MB）")
        try:
            # 解析（zip/XML）是 CPU 阻塞操作，放线程池避免卡住事件循环。
            text = await run_in_threadpool(parse_file, file.filename, data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # noqa: BLE001  损坏 docx / 非法 XML 等
            raise HTTPException(400, f"文件解析失败：{e}") from e
        source_file = file.filename
    elif raw_text and raw_text.strip():
        text = raw_text
    else:
        raise HTTPException(400, "请上传文件（.txt/.md/.docx）或粘贴原文")
    if len(text.strip()) < 20:
        raise HTTPException(400, "导入的文本内容太短（至少 20 字）")
    name = (title or "").strip() or Path(source_file or "未命名").stem
    p = deps.store().create_project(
        title=name[:120],
        adaptation_type=normalize_adaptation_type(adaptation_type),
        language=language,
        raw_text=text,
    )
    common.persist_original(p)
    # 画风与改编类型正交（漫剧 = 短剧 + 二次元画风），存进项目风格，供美术/
    # 定妆图/选角读取。与 adaptation_type 分开存，避免「一个画风一个类型」的组合爆炸。
    deps.store().merge_video_style(p.id, {"visual_style": normalize_visual_style(visual_style)})
    # 作者风格只做规则画像（无网络调用），避免创建流程被 LLM 阻塞
    try:
        style = await run_in_threadpool(extract_author_style, text)
        deps.store().set_project_notes(p.id, f"作者风格：{style.get('summary', '')}")
    except Exception:
        pass
    conv = deps.store().ensure_default_conversation(p.id)
    genres = detect_genres(text, top=2)
    return {
        "id": p.id,
        "title": p.title,
        "adaptation_type": p.adaptation_type,
        "conversation_id": conv.id,
        "raw_len": len(text),
        "genres": genres,
        "source_file": source_file,
        "warnings": [],
    }


# ---------- 工作目录落盘 ----------


@router.post("/projects/{project_id}/structure")
def project_structure(project_id: str) -> dict[str, Any]:
    """把某项目的原稿与最新版本落盘到工作目录，返回目录树文本。"""
    p = common.get_project(project_id)
    ws = common.workspace()
    if not ws.persist:
        raise HTTPException(400, "当前为「仅应用内」模式，不写入磁盘；可切换到落盘模式后重试")
    if not ws.root:
        raise HTTPException(400, "工作目录未配置，请先设置工作目录")
    common.persist_original(p)
    latest = deps.store().latest_version(p)
    if latest:
        common.persist_version(p, latest)
    return {"project_id": project_id, "structure": ws.tree_text(p.title), "root": ws.info()["root"]}


# ---------- 知识 / 笔记 ----------


@router.get("/projects/{project_id}/notes")
def get_notes(project_id: str) -> dict[str, Any]:
    common.get_project(project_id)
    return {"project_id": project_id, "notes": deps.store().get_project_notes(project_id)}


@router.put("/projects/{project_id}/notes")
def set_notes(project_id: str, payload: NotesSet) -> dict[str, Any]:
    p = common.get_project(project_id)
    deps.store().set_project_notes(project_id, payload.notes)
    # 同步到工作目录的「04_知识库」——失败不影响主流程。
    ws = common.workspace()
    if ws.configured:
        try:
            ws.save_note(p.title, "编剧圣经_设定.md", payload.notes)
        except Exception:  # noqa: BLE001
            pass
    return {"project_id": project_id, "ok": True}


# ---------- 本地剧本文件（查看 / 下载）----------


def _file_mime(ext: str) -> str:
    ext = (ext or "").lower()
    if ext in {".txt", ".md", ".markdown"}:
        return "text/plain; charset=utf-8"
    if ext == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


@router.get("/projects/{project_id}/files")
def list_project_files(project_id: str) -> dict[str, Any]:
    """列出某项目在磁盘上的剧本文件（按 01_原稿/02_版本/03_导出/04_知识库 分组）。"""
    p = common.get_project(project_id)
    ws = common.workspace()
    if not ws.persist:
        return {
            "project_id": project_id,
            "persist": False,
            "configured": False,
            "root": None,
            "folders": [],
        }
    data = ws.list_project_files(p.title)
    return {
        "project_id": project_id,
        "persist": True,
        "configured": True,
        "root": (data or {}).get("root"),
        "folders": (data or {}).get("folders", []),
    }


@router.get("/projects/{project_id}/files/{relpath:path}")
def get_project_file(project_id: str, relpath: str) -> FileResponse:
    """读取 / 预览某项目下的一个剧本文件（文本内联预览，其它附件下载）。"""
    p = common.get_project(project_id)
    ws = common.workspace()
    if not ws.persist:
        raise HTTPException(400, "当前为「仅应用内」模式，未落盘文件")
    path = ws.resolve_file(p.title, relpath)
    if not path:
        raise HTTPException(404, "文件不存在")
    ext = path.suffix.lower()
    inline = ext in {".txt", ".md", ".markdown"}
    headers = common.content_disposition(path.name, inline=inline)
    return FileResponse(path, media_type=_file_mime(ext), headers=headers)
