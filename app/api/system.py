# =====================================================================
# system.py —— 运行状态、后台任务、工作目录
#
# 这些接口不属于某个业务域，而是「运行环境」的读写。
# =====================================================================

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..workspace import SUBDIRS, configure_root, pick_directory
from . import common, deps
from .schemas import WorkspaceSet

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/status")
def status() -> dict[str, Any]:
    """返回当前运行配置摘要（供前端展示「模型 / 存储」状态）。"""
    cfg = deps.settings()
    info = deps.llm().describe()
    # 生图模型也要露出来：资产库的「生成资产图」走的就是它，
    # 用户在设置里绑好的那个模型，得看得见才敢用。
    from ..llm.registry import resolve_image

    img = resolve_image(cfg, deps.store())
    return {
        "model": {
            "available": info["available"],
            "provider": info["provider_label"],
            "name": info["model"] or "-",
            "source": info["source"],
        },
        "vision": info["vision"],
        "image": {
            "available": bool(img and img.api_key),
            "provider": (img.provider if img else "") or "",
            "label": (img.label if img else "") or "",
            "model": (img.model if img else "") or "",
            "source": (img.source if img else "none") or "none",
        },
        "storage": {
            "database": cfg.database_url.split("://")[0],
            "checkpointer": cfg.checkpointer,
        },
        "workspace": {
            "persist": cfg.workspace_persist,
            "root": cfg.effective_workspace_root,
        },
        "mode": "full" if info["available"] else "demo",
        "langsmith": bool(cfg.langsmith_tracing and cfg.langsmith_api_key),
    }


# ---------- 后台任务 ----------


def _all_tasks(limit: int = 50) -> list[dict[str, Any]]:
    """合并「内存中的进行态」与「落库的历史态」：同一 id 以内存态为准。"""
    merged: dict[str, dict[str, Any]] = {}
    try:
        for row in deps.store().list_subagent_tasks(limit=limit):
            merged[row["id"]] = row
    except Exception:  # noqa: BLE001  落库不可用时不阻断任务面板
        pass
    for task in deps.subagent_runner().list_tasks():
        merged[task.id] = task.to_dict()
    items = sorted(merged.values(), key=lambda t: t.get("created_at") or "", reverse=True)
    return items[:limit]


@router.get("/tasks")
def list_tasks(active_only: bool = False) -> list[dict[str, Any]]:
    """列出后台任务（默认全部，active_only=true 只返回运行中的）。"""
    items = _all_tasks()
    if active_only:
        items = [t for t in items if t.get("status") in ("pending", "running")]
    return items


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    """查询单个后台任务详情（内存优先，其次查历史快照）。"""
    task = deps.subagent_runner().get(task_id)
    if task:
        return task.to_dict()
    row = deps.store().get_subagent_task(task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    return row


# ---------- 工作目录（真实关联磁盘文件夹）----------


@router.get("/workspace")
def get_workspace() -> dict[str, Any]:
    """返回当前工作目录配置（含根路径、落盘模式与目录结构说明）。"""
    ws = common.workspace()
    info = ws.info(deps.settings().effective_workspace_root)
    # 已配置时附带一份结构说明文字。
    info["structure"] = (
        "\n".join(f"{code}/  <-  {label}" for code, label in SUBDIRS)
        if ws.configured
        else None
    )
    return info


@router.get("/workspace/select")
def select_workspace_directory() -> dict[str, Any]:
    """弹出系统原生文件夹选择对话框（Windows），选择即设为工作目录。

    注意：这是**阻塞式**调用——服务端会弹出一个原生「选择文件夹」窗口，等用户
    选完返回。取消时返回 cancelled=True，不改变工作目录。
    """
    ws = common.workspace()
    initial = str(ws.root) if ws.root else None
    try:
        path = pick_directory(initial)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    if not path:
        return {"cancelled": True, "path": None}
    # 选择文件夹即切到「落盘」模式。
    ws = configure_root(path, persist=True)
    return {"cancelled": False, "path": path, **ws.info(deps.settings().effective_workspace_root)}


@router.post("/workspace")
def set_workspace(payload: WorkspaceSet) -> dict[str, Any]:
    """设置 / 更新工作目录。persist=False 表示「仅应用内」模式（不写磁盘，数据库照常存）。"""
    root = (payload.root or "").strip()
    try:
        ws = configure_root(root, payload.persist)
        if ws.persist:
            ws.ensure_root()
            ws.write_readme()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"无法创建工作目录：{e}") from e
    return {"ok": True, **ws.info(deps.settings().effective_workspace_root)}
