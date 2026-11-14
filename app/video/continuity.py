# =====================================================================
# continuity.py —— 分镜连续性解析
#
# 导演产出「连续性计划」：每个镜头带 reference_group（共享环境参考分组）
# 与 chain_from（首尾帧接力，指向前序镜头的 order）。本模块在镜头成片后，
# 把这些符号化计划解析成运行时输入（reference_videos），从而让独立生成的
# 镜头之间共享视觉锚点，解决「分镜导致场景/人物不统一」的问题。
#
# 说明：MiniMax 等 provider 的参考素材需要公开 URL。成片后的 video_url
# 正好是公开直链，因此无需额外图片托管即可实现「上一镜成片作为下一镜参考」。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def resolve_continuity(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把连续性计划解析成运行时输入，原地补齐每个镜头的 reference_videos。

    规则：
      1. 每个 reference_group 内，以「组内第一个已成片镜头」为锚点，
         该组后续镜头追加锚镜的 video_url 作为参考视频（锁场景/光影/人物）。
      2. chain_from=N：追加第 N 个镜头（order=N）的成片 URL 作为参考视频，
         实现首尾帧接力 / 风格锁定。

    无成片 URL 的镜头被跳过（不会把自己或空值当作参考）。
    """
    order_to_url: dict[int, str] = {
        int(s.get("order", -1)): s["video_url"]
        for s in shots
        if s.get("video_url")
    }

    # 组内锚点：reference_group -> 组内最先成片的镜头 order。
    group_anchor: dict[str, int] = {}
    for s in sorted(shots, key=lambda x: int(x.get("order", 0))):
        g = str(s.get("reference_group") or "")
        if g and s.get("video_url") and g not in group_anchor:
            group_anchor[g] = int(s["order"])

    for s in shots:
        refs: list[str] = list(s.get("reference_videos") or [])
        # 首尾帧接力：链到更早镜头的成片。
        chained = ""
        chain = s.get("chain_from")
        if chain is not None:
            chained = order_to_url.get(int(chain), "")
            if chained and chained not in refs:
                refs.append(chained)
        # 环境参考：同组锚镜（且不是自己）——**只在接力断链时补位**。
        # 两条接力视频=两个不同机位的运镜信号互相打架（果园 2026-11-14 实测：
        # 单元 3 挂了 chain(单元2) + 组锚(单元1) 两条，单元 1 成片里的首句
        # 台词与踱步运镜被模型照着又演一遍）；chain 的前镜成片本来就同组同
        # 光线同人物，外观锚已随链继承，组锚是纯冗余的干扰源。
        g = str(s.get("reference_group") or "")
        anchor = group_anchor.get(g)
        if not chained and anchor is not None and anchor != int(s.get("order", -1)):
            url = order_to_url.get(anchor)
            if url and url not in refs:
                refs.append(url)
        if refs:
            s["reference_videos"] = refs

    return shots


def resolve_submission_references(
    *,
    shot_id: str,
    shot_plan: list[dict[str, Any]],
    completed_urls: dict[str, str],
    image_registry: dict[str, str] | None = None,
    script: Any = None,
) -> dict[str, list[str]]:
    """提交单镜生成任务时，解析该镜应携带的多模态一致性输入。

    - reference_videos：resolve_continuity 依据「已完成前镜的成片 URL」
      解析接力（chain_from）与环境参考（reference_group）；
    - reference_images：风格指南注册表里的定妆图，按「场景→环境图、
      人物→角色图」回填（app.media.refs.apply_reference_images）。

    任何一步失败都只降级为空列表，不阻断任务提交。

    返回 {"reference_images": [...], "reference_videos": [...]}。
    """
    # ---- 成片接力：把已完成的 video_url 覆盖进镜头计划再解析 ----
    # 组任务只挂在组头（组头 shot_id 建档）：成员镜头查不到自己的成片时，
    # 回落到组头那条——一条组视频覆盖全组。没有这层映射，chain_from 指向
    # 上一组成员时永远查不到 URL，接力链在组化方案上整段断掉（2026-11-14）。
    group_head_url: dict[str, str] = {}
    by_group: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in shot_plan or []:
        if isinstance(raw, dict) and raw.get("group_id"):
            by_group.setdefault(
                (str(raw.get("scene_id") or ""), str(raw["group_id"])), []
            ).append(raw)
    for members in by_group.values():
        head = min(members, key=lambda m: (int(m.get("group_index") or 0),
                                           int(m.get("order") or 0)))
        head_url = completed_urls.get(str(head.get("id") or head.get("shot_id") or ""))
        if head_url:
            for m in members:
                group_head_url[str(m.get("id") or m.get("shot_id") or "")] = head_url

    plan: list[dict[str, Any]] = []
    for raw in shot_plan or []:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("id") or raw.get("shot_id") or "")
        plan.append({
            "order": raw.get("order", 0),
            "id": sid,
            "reference_group": raw.get("reference_group") or "",
            "chain_from": raw.get("chain_from"),
            "video_url": completed_urls.get(sid) or group_head_url.get(sid),
        })

    videos: list[str] = []
    if any(p.get("video_url") for p in plan):
        resolve_continuity(plan)
        me = next((p for p in plan if p.get("id") == shot_id), None)
        videos = list((me or {}).get("reference_videos") or [])

    # ---- 定妆图：按场景/人物名称查注册表回填 ----
    images: list[str] = []
    registry = image_registry or {}
    raw_me = next(
        (r for r in shot_plan or [] if isinstance(r, dict)
         and str(r.get("id") or r.get("shot_id") or "") == shot_id),
        None,
    )
    if registry and script is not None and raw_me is not None:
        try:
            from ..domain import Shot
            from ..media.refs import apply_reference_images

            shot = Shot.model_validate(dict(raw_me))
            if apply_reference_images([shot], registry, script):
                images = list(shot.reference_images or [])
        except Exception as e:  # noqa: BLE001
            log.warning("定妆参考图回填失败（忽略，不影响提交）：%s", e)

    return {"reference_images": images, "reference_videos": videos}
