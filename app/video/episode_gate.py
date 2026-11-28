# =====================================================================
# episode_gate.py —— 按集推进的硬闸门（2026-11-27）
#
# 用户要求 Agent 严格一集一集往下跑：剧本 → 分镜 → 提示词 → 出片，
# 当前集没有完成，不允许把下一集也拆/也出。这里只做「状态判定 + 前进」，
# 不碰模型、不花钱；工具层和 UI 都调它。
#
# 完成判据（宁可严，不可松）：
#   - 当前集在最新镜头方案里至少有一个生成单元；
#   - 每个镜头都有 video_prompt；
#   - 每个生成单元都已批准（组头 approved、组内无 needs_revision）；
#   - 每个生成单元都有一条 succeeded 且带成片 URL 的任务。
# 满足后 `advance` 才能 +1；后退（回看已完成的集）允许。
# =====================================================================

from __future__ import annotations

from typing import Any


def episode_numbers(script: Any) -> list[int]:
    """剧本里实际出现的集号（升序）；没有分集时按 1 集算。"""
    eps = sorted({int(getattr(sc, "episode", 1) or 1) for sc in getattr(script, "scenes", []) or []})
    return eps or [1]


def episode_scene_ids(script: Any, episode: int) -> list[str]:
    return [
        sc.id for sc in (getattr(script, "scenes", []) or [])
        if int(getattr(sc, "episode", 1) or 1) == int(episode)
    ]


def _successful_units(store: Any, project_id: str, episode: int, version: Any,
                      units: list[dict[str, Any]], jobs: list[Any]) -> dict[str, str]:
    """只认本集版本中、与当前生成单元提示词一致的成功成片。"""
    if version is None:
        return {}
    version_ids = {v.id for v in store.list_video_versions(project_id, episode=episode)}
    if not version_ids:
        version_ids = {version.id}  # 旧整片版本的兼容路径
    prompts = {u["shot_id"]: str(u.get("prompt") or "").strip() for u in units}
    out: dict[str, str] = {}
    for job in jobs:
        if (job.status == "succeeded" and job.video_url
                and job.shot_id in prompts and prompts[job.shot_id]
                and str(job.prompt or "").strip() == prompts[job.shot_id]
                and (not job.version_id or job.version_id in version_ids)):
            out.setdefault(job.shot_id, job.video_url)
    return out


def active_episode_status(store: Any, project: Any) -> dict[str, Any]:
    """当前集走到哪一步 + 能不能前进。返回可直接给 Agent/UI 的字典。"""
    from .service import generation_units, unit_approved

    active = store.get_active_episode(project.id)
    version = store.latest_version(project)
    script = version.script if version is not None else None
    eps = episode_numbers(script)
    if active not in eps:
        active = eps[0] if eps else 1
    scene_ids = set(episode_scene_ids(script, active)) if script is not None else set()

    vv = store.latest_video_version(project, episode=active)
    shots = [
        s for s in (getattr(vv, "shots", []) or [])
        if isinstance(s, dict) and s.get("id") and s.get("scene_id") in scene_ids
    ]
    units = generation_units(shots) if shots else []
    prompt_ok = bool(shots) and all(str(s.get("video_prompt") or "").strip() for s in shots)
    approved_ok = bool(units) and all(unit_approved(shots, u) for u in units)
    heads = {u["shot_id"] for u in units}
    succeeded = set(_successful_units(
        store, project.id, active, vv, units, store.list_video_jobs(project.id)
    ))
    generated_ok = bool(heads) and heads <= succeeded

    blockers: list[str] = []
    if not scene_ids:
        blockers.append(f"第 {active} 集还没有剧本场次")
    if not shots:
        blockers.append(f"第 {active} 集还没有分镜方案")
    else:
        missing_prompt = sum(1 for s in shots if not str(s.get("video_prompt") or "").strip())
        if missing_prompt:
            blockers.append(f"第 {active} 集还有 {missing_prompt} 镜没写提示词")
        if not approved_ok:
            approved_n = sum(1 for u in units if unit_approved(shots, u))
            blockers.append(f"第 {active} 集提示词已批准 {approved_n}/{len(units)} 个生成单元")
        if not generated_ok:
            blockers.append(f"第 {active} 集已出片 {len(succeeded)}/{len(heads)} 个生成单元")
    complete = bool(scene_ids) and bool(shots) and prompt_ok and approved_ok and generated_ok

    nxt = active + 1 if active < max(eps) else None
    return {
        "active": active,
        "episodes": eps,
        "next": nxt,
        "scenes": len(scene_ids),
        "shots": len(shots),
        "units": len(units),
        "prompts_ready": prompt_ok,
        "approved": approved_ok,
        "generated": generated_ok,
        "complete": complete,
        "blockers": blockers,
    }


def can_advance(store: Any, project: Any) -> tuple[bool, str, dict[str, Any]]:
    status = active_episode_status(store, project)
    if status["next"] is None:
        return False, "已经是最后一集了", status
    if not status["complete"]:
        why = "；".join(status["blockers"]) or "当前集还没完成"
        return False, f"先完成第 {status['active']} 集：{why}", status
    return True, f"第 {status['active']} 集已完成，可以进入第 {status['next']} 集", status


def advance_episode(store: Any, project: Any) -> tuple[bool, str, dict[str, Any]]:
    ok, msg, status = can_advance(store, project)
    if ok:
        store.set_active_episode(project.id, status["next"])
        status = active_episode_status(store, project)
        msg = f"已进入第 {status['active']} 集"
    return ok, msg, status


def switch_episode(store: Any, project: Any, episode: int) -> tuple[bool, str, dict[str, Any]]:
    """回看允许；前进只允许 +1 且当前集已完成，不能跳集。"""
    status = active_episode_status(store, project)
    target = int(episode)
    if target == status["active"]:
        return True, f"已经在第 {target} 集", status
    if target < status["active"]:
        if target not in status["episodes"]:
            return False, f"没有第 {target} 集", status
        store.set_active_episode(project.id, target)
        return True, f"已回到第 {target} 集", active_episode_status(store, project)
    if target != status["active"] + 1:
        return False, f"不能跳集：当前第 {status['active']} 集，只能先进入第 {status['active'] + 1} 集", status
    ok, msg, status2 = can_advance(store, project)
    if not ok:
        return False, msg, status2
    store.set_active_episode(project.id, target)
    return True, f"已进入第 {target} 集", active_episode_status(store, project)


def episode_statuses(store: Any, project: Any) -> dict[str, Any]:
    """分集视频父页用：每集的状态卡（场/镜/单元/批准/出片/完成）。"""
    from .service import generation_units, unit_approved

    active = store.get_active_episode(project.id)
    version = store.latest_version(project)
    script = version.script if version is not None else None
    eps = episode_numbers(script)
    jobs = store.list_video_jobs(project.id)
    rows: list[dict[str, Any]] = []
    for ep in eps:
        scene_ids = set(episode_scene_ids(script, ep)) if script is not None else set()
        vv = store.latest_video_version(project, episode=ep)
        shots = [
            s for s in (getattr(vv, "shots", []) or [])
            if isinstance(s, dict) and s.get("scene_id") in scene_ids
        ]
        units = generation_units(shots) if shots else []
        heads = {u["shot_id"] for u in units}
        approved_n = sum(1 for u in units if unit_approved(shots, u))
        succeeded_urls = _successful_units(store, project.id, ep, vv, units, jobs)
        succeeded = set(succeeded_urls)
        prompt_n = sum(1 for s in shots if str(s.get("video_prompt") or "").strip())
        complete = bool(
            scene_ids and shots and units
            and prompt_n == len(shots)
            and approved_n == len(units)
            and heads <= succeeded
        )
        preview_url = next(
            (succeeded_urls[str(s.get("id"))] for s in shots
             if str(s.get("id") or "") in succeeded_urls),
            "",
        )
        rows.append({
            "episode": ep,
            "active": ep == active,
            "scenes": len(scene_ids),
            "shots": len(shots),
            "units": len(units),
            "duration_sec": round(sum(float(s.get("duration_sec") or 0) for s in shots), 1),
            "prompts": prompt_n,
            "approved": approved_n,
            "generated": len(succeeded & heads),
            "preview_url": preview_url,
            "complete": complete,
            "started": bool(shots),
        })
    return {"active": active, "episodes": rows}
