# =====================================================================
# production.py —— 生产工作流 API
#
#   POST /projects/{id}/production   启动「小说 → 提示词」整条流水线
#   GET  /projects/{id}/production   读最近一次流水线的结果快照
#
# 为什么走后台任务：五个阶段（剧本 / 拆解 / 风格 / 资产图 / 提示词）要跑
# 几十次模型调用，HTTP 请求等不起。启动即返回 task_id，进度与结果经
# SubAgentRunner 落库，前端用 /api/tasks 轮询（与剧组工具同一套面板）。
# =====================================================================

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from . import deps

router = APIRouter()
log = logging.getLogger(__name__)


def _start_pipeline(project_id: str, limit: int = 0) -> dict[str, object]:
    """起一条后台生产流水线（确认卡与 REST 入口共用）。"""
    store = deps.store()
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    from ..agent.production import STAGE_LABELS, run_production

    runner = deps.subagent_runner()

    def _run(task) -> str:
        result = run_production(
            store, deps.llm(), deps.settings(), project_id,
            on_progress=lambda stage, msg: runner.update_step(task.id, msg, "running"),
            limit=int(limit or 0),
        )
        return "；".join(result.get("notes") or [])

    task_id = runner.start("生产流水线（小说→提示词）", _run, project_id=project_id)
    return {
        "task_id": task_id,
        "stages": [{"key": k, "label": v} for k, v in STAGE_LABELS.items()],
    }


@router.post("/projects/{project_id}/production")
def start_production(project_id: str, limit: int = 0) -> dict[str, object]:
    """启动生产流水线（后台）。limit > 0 时只拆前 N 场戏（限场续跑）。"""
    return _start_pipeline(project_id, limit)


@router.post("/production/approvals/{approval_id}/confirm")
def confirm_production(approval_id: str) -> dict[str, object]:
    """确认并启动生产流水线（预算闸门的确认动作）。

    生产流水线要花钱（模型调用 + 逐张生图），所以 Agent 只能**发起**
    （start_production 工具），真正开跑由用户点确认卡触发——服务端硬闸门，
    模型伪造不了（同 D-43 的出片批准）。
    """
    store = deps.store()
    approval = store.get_pending_approval(approval_id)
    if approval is None:
        raise HTTPException(404, "确认请求不存在或已过期")
    if approval.status != "pending":
        raise HTTPException(409, f"这条确认请求已经是「{approval.status}」了")
    if not str(approval.note or "").startswith("production"):
        raise HTTPException(400, "这不是生产流水线的确认请求")
    limit = 0
    for item in approval.shot_ids:
        if str(item).startswith("limit="):
            try:
                limit = int(str(item).split("=", 1)[1])
            except ValueError:
                limit = 0
    store.resolve_pending_approval(approval_id, "confirmed")
    started = _start_pipeline(approval.project_id, limit)
    return {"ok": True, "approval_id": approval_id, **started}


@router.post("/projects/{project_id}/reference-assets")
def start_reference_assets(project_id: str) -> dict[str, object]:
    """用生图模型给资产库生成定妆照 / 场景图（后台任务）。

    输入框「＋」里的「生成资产图」走这里：用户要的是「点一下就把资产补齐」，
    不是在对话里描述一遍再让 Agent 决定。
    """
    store = deps.store()
    p = store.get_project(project_id)
    if p is None:
        raise HTTPException(404, "作品不存在")
    version = store.latest_version(p)
    if version is None:
        raise HTTPException(400, "还没有剧本版本，先在对话里生成初稿")

    from ..domain import StyleGuide
    from ..media.refs import build_reference_assets

    runner = deps.subagent_runner()
    style_raw = store.get_video_style(p.id)
    guide = StyleGuide.model_validate(style_raw) if style_raw else None

    def _run(task: object) -> str:
        def _progress(msg: str) -> None:
            runner.update_step(task.id, msg, "running")

        refs = build_reference_assets(version.script, guide, on_progress=_progress)
        from ..media.looks import register_generated_images
        store.merge_video_style(p.id, register_generated_images(store.get_video_style(p.id) or {}, version.script, refs))
        return f"已生成 {len(refs)} 张资产图：{'、'.join(refs)}"

    task_id = runner.start("生成资产图", _run, project_id=p.id)
    return {"task_id": task_id, "started": True}


@router.get("/projects/{project_id}/production")
def production_status(project_id: str) -> dict[str, object]:
    """最近一次生产任务的状态 + 这次再跑要动多少资源（预估）。

    按 project_id 过滤：任务面板是全局的，但生产任务是**作品级**的——
    不按作品过滤的话，A 作品会读到 B 作品刚跑的那次生产流水线结果
    （此前此函数忽略 project_id，返回全局最近一次）。
    """
    from ..agent.production import estimate_production

    runner = deps.subagent_runner()
    status: dict[str, object] = {"task_id": None, "status": "idle", "result": None}
    for t in runner.list_tasks(project_id=project_id):
        if t.name.startswith("生产流水线"):
            status = {"task_id": t.id, "status": t.status, "result": t.result}
            break
    status["estimate"] = estimate_production(deps.store(), project_id)
    return status
