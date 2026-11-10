# =====================================================================
# common.py —— API 层共享辅助
#
# 路由之间复用的序列化、错误处理与「落盘不阻断主流程」的写入逻辑。
# 约定：这些函数只做编排与格式转换，不承载业务规则——业务在
# pipeline / agent / store 里。
# =====================================================================

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException

from ..domain import Script
from ..pipeline.export import script_to_screenplay
from ..pipeline.patch import validate_script
from ..workspace import current_workspace
from . import deps


def now() -> datetime:
    return datetime.now(UTC)


def get_project(project_id: str) -> Any:
    """获取项目，不存在则 404。"""
    p = deps.store().get_project(project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


def content_disposition(filename: str, *, inline: bool = False) -> dict[str, str]:
    """构造 RFC 5987 Content-Disposition 头（中文文件名兼容）。

    `filename=` 是给老客户端的纯 ASCII 退路：中文标题会被替换成下划线。
    收敛规则：连续下划线压成一个、去掉首尾下划线；如果去掉扩展名后连一个
    ASCII 字母数字都不剩（纯中文标题就是这种情况），退成 `script<扩展名>`，
    而不是「____.docx」这种谁也读不出来的名字。真名由 `filename*` 承载。
    """
    suffix = Path(filename).suffix or ""
    stem = Path(filename).stem if suffix else filename
    ascii_stem = re.sub(r"[^\x20-\x7e]", "_", stem)
    ascii_stem = re.sub(r"_{2,}", "_", ascii_stem).strip("_ ")
    if not re.search(r"[A-Za-z0-9]", ascii_stem):
        ascii_stem = "script"
    ascii_name = ascii_stem + re.sub(r"[^\x20-\x7e]", "", suffix)
    encoded = quote(filename, safe="")
    disp = "inline" if inline else "attachment"
    return {"Content-Disposition": f'{disp}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'}


def script_to_dict(script: Script) -> dict[str, Any]:
    data = script.model_dump(exclude_none=True)
    data["validation"] = [i.model_dump() for i in validate_script(script)]
    return data


def project_dict(project: Any) -> dict[str, Any]:
    st = deps.store()
    runs = st.list_agent_runs(project.id)
    latest_run = runs[0] if runs else None
    map_chapters = len((project.novel_map or {}).get("chapters") or [])
    return {
        "id": project.id,
        "title": project.title,
        "adaptation_type": project.adaptation_type,
        "language": project.language,
        "status": project.status,
        "current_version_id": project.current_version_id,
        "created_at": project.created_at.isoformat(),
        "version_count": len(st.list_versions(project.id)),
        "run_count": len(runs),
        # 长篇连载：已改编到第几章 + 地图章节数（完整地图只在详情接口给）
        "adapted_until": int(getattr(project, "adapted_until", 0) or 0),
        "novel_map_chapters": map_chapters,
        "latest_run": {"status": latest_run.status, "updated_at": latest_run.updated_at.isoformat()}
        if latest_run
        else None,
    }


def project_overview(project: Any) -> dict[str, Any]:
    """作品的「消耗 / 产出」概览（C7 最小版，对标 mediago-drama 的用量统计）。

    只聚合**已经落库**的数据，不新增任何采集点——
    镜头/成片数来自 video_version + video_jobs，预估费用是提交时按秒数
    算好落库的 cost_estimate（只对**真正提交到服务商**的任务求和：没有
    external_task_id 的任务压根没出门，不该算进"花了多少"）。
    数字全部是创作者语言（场/镜/成片/定妆图），**不出现 token**——
    D-24：界面禁工程黑话；BYOK 用户真要逐笔对账，厂商控制台比这里的
    预估准，所以这里只回答「这部作品走到哪、大概花了多少」。
    """
    st = deps.store()
    pid = project.id

    version = st.latest_version(project)
    script = version.script if version else None

    # 镜头方案：最新视频版本
    versions = sorted(st.list_video_versions(pid), key=lambda v: v.created_at)
    vv = versions[-1] if versions else None
    shots = [s for s in (vv.shots if vv else []) if isinstance(s, dict) and s.get("id")]

    # 任务一次取全，下面三种口径都从这份派生：
    all_jobs = st.list_video_jobs(pid)
    # 每个镜头的最新一次任务（后写覆盖先写——与工具层 latest_job_by_shot 同一读法）
    latest_job: dict[str, Any] = {}
    for j in sorted(all_jobs, key=lambda x: x.created_at):
        if j.shot_id:
            latest_job[j.shot_id] = j

    shots_approved = sum(1 for s in shots if s.get("prompt_status") == "approved")
    shots_done = sum(1 for s in shots if getattr(latest_job.get(s["id"]), "status", "") == "succeeded")
    shots_failed = sum(1 for s in shots if getattr(latest_job.get(s["id"]), "status", "") in ("failed", "timeout"))

    # 花费统计所有真正出过门的任务（拿到过 external_task_id = 服务商收了单）：
    # 重 roll 的每一版都各自花了钱，不能只按镜头最新版算——那样会把
    # 历史消耗低估。没出门的任务（还没投递成功）不计。
    submitted = [j for j in all_jobs if j.external_task_id]
    cost_total = sum(float(j.cost_estimate or 0) for j in submitted if j.cost_estimate)

    # 视觉资产登记数（按实体去重：一个角色一张定妆图算一个）
    style = st.get_video_style(pid) or {}
    ref_images = [u for u in (style.get("reference_images") or {}).values() if u]
    voices = [v for v in (style.get("voice_casting") or {}).values() if v]

    # 合成状态：finalize 记录活在内存（重启后为空是真实情况，不假装还活着——
    # 与 app/agent/tasks.py 同一口径）
    finalized: dict[str, Any] = {}
    try:
        rec = deps.finalize_manager().latest_for_project(pid)
        if rec and rec.get("status") == "succeeded":
            result = rec.get("result") or {}
            finalized = {
                "duration_sec": float(result.get("duration_sec") or 0),
                "dubbed": bool(result.get("dubbed")),
                "subtitled": bool(result.get("subtitled")),
            }
    except Exception:  # noqa: BLE001  合成模块不可用不该拖垮概览
        pass

    return {
        "scenes": len(script.scenes) if script else 0,
        "characters": len(script.characters) if script else 0,
        "shots": len(shots),
        "shots_approved": shots_approved,
        "shots_done": shots_done,
        "shots_failed": shots_failed,
        "reference_images": len(set(ref_images)),
        "voices": len(set(voices)),
        "video_submissions": len(submitted),
        "cost_estimate_usd": round(cost_total, 4),
        "finalized": finalized or None,
    }


def version_dict(version: Any, *, with_content: bool = False) -> dict[str, Any]:
    data = {
        "id": version.id,
        "project_id": version.project_id,
        "parent_version_id": version.parent_version_id,
        "source_type": version.source_type,
        "label": version.label,
        "notes": version.notes,
        "milestone": getattr(version, "milestone", None),
        "created_at": version.created_at.isoformat(),
    }
    if with_content:
        script_dict = script_to_dict(version.script)
        data["script"] = script_dict
        data["validation"] = script_dict.get("validation", [])
    return data


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """把事件封装成 SSE 文本帧。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------- 工作目录写入（失败不阻断主流程）----------


def workspace() -> Any:
    """取当前工作目录实例（未配置 / 仅应用内时，写文件会静默跳过）。"""
    cfg = deps.settings()
    return current_workspace(cfg.effective_workspace_root, cfg.workspace_persist)


def proj_title(raw_title: str) -> str:
    """把任意标题清洗成适合做文件名的安全文本。"""
    text = str(raw_title or "").strip() or "未命名剧本"
    text = re.sub(r'[<>:"\\|?*]', "_", text)
    return text[:80] or "未命名剧本"


def persist_original(project: Any) -> None:
    """把项目原著写入工作目录的「01_原稿」。"""
    ws = workspace()
    if not ws.configured:
        return
    try:
        ws.save_original(project.title, proj_title(project.title) + ".txt", project.raw_text)
    except Exception:  # noqa: BLE001
        pass  # 落盘失败不影响主流程


def persist_version(project: Any, version: Any, *, label: str | None = None) -> None:
    """把一份剧本版本写入工作目录的「02_版本」。"""
    ws = workspace()
    if not ws.configured:
        return
    try:
        text = script_to_screenplay(version.script)
        name = f"{proj_title(project.title)}_v{(version.label or label or '').strip() or 'version'}"
        ws.save_version(project.title, name, text)
    except Exception:  # noqa: BLE001
        pass
