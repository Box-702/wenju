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


@router.post("/projects/{project_id}/production")
def start_production(project_id: str) -> dict[str, object]:
    store = deps.store()
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
    from ..agent.production import STAGE_LABELS, run_production

    runner = deps.subagent_runner()

    def _run(task) -> str:
        result = run_production(
            store, deps.llm(), deps.settings(), project_id,
            on_progress=lambda stage, msg: runner.update_step(task.id, msg, "running"),
        )
        return "；".join(result.get("notes") or [])

    task_id = runner.start("生产流水线（小说→提示词）", _run, project_id=project_id)
    return {
        "task_id": task_id,
        "stages": [{"key": k, "label": v} for k, v in STAGE_LABELS.items()],
    }


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

    from ..agent.tasks import SubAgentRunner
    from ..domain import StyleGuide
    from ..media.refs import build_reference_assets

    runner = deps.subagent_runner()
    style_raw = store.get_video_style(p.id)
    guide = StyleGuide.model_validate(style_raw) if style_raw else None

    def _run(task: object) -> str:
        def _progress(msg: str) -> None:
            runner.update_step(task.id, msg, "running")

        refs = build_reference_assets(version.script, guide, on_progress=_progress)
        store.merge_video_style(p.id, {"reference_images": refs})
        return f"已生成 {len(refs)} 张资产图：{'、'.join(refs)}"

    task_id = runner.start("生成资产图", _run, project_id=p.id)
    return {"task_id": task_id, "started": True}


@router.get("/projects/{project_id}/production")
def production_status(project_id: str) -> dict[str, object]:
    """最近一次生产任务的状态。

    说明：SubAgentTask 不带 project_id（任务面板是全局的），这里返回的是
    **全局最近一次**生产任务——单机单用户场景下即当前用户刚点的那次。
    """
    from ..agent.tasks import get_runner

    runner = get_runner()
    for t in runner.list_tasks():
        if t.name.startswith("生产流水线"):
            return {"task_id": t.id, "status": t.status, "result": t.result}
    return {"task_id": None, "status": "idle", "result": None}
