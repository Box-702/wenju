# =====================================================================
# usage.py —— 出片用量摘要（2026-11-14 产品口径，D-53）
#
# 用户界面不再展示费用估算（¥/USD）：预估与实付脱节只会伤信任
# （导入回执「预估 ¥3.6」vs 实付 ¥27.7 的教训，见 DEVELOPMENT T-03/T-05）。
# 界面只展示三类创作者语言的数量——
#   · 预期生成多少秒视频（各单元/任务的计划时长）
#   · 参考视频多长（手挑素材实测 + 接力锚溯源；拿不到只计条数，不编数）
#   · 参考图多少张
# 逐笔费用对账走厂商控制台与 docs/DEVELOPMENT §7 账本，不进用户界面。
# =====================================================================

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPLAY_PREFIX = "/api/assets/file/"


def _asset_duration(store: Any, url: str) -> float:
    """本机素材（/api/assets/file/{id} 回放地址）的实测时长；探不到返回 0。"""
    asset_id = url.rsplit("/", 1)[-1]
    try:
        import tempfile

        from .finalize import _media_duration_sec

        asset = store.get_asset(asset_id)
        data = getattr(asset, "content", None) if asset is not None else None
        if not data:
            return 0.0
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(data)
            tmp = Path(f.name)
        try:
            return _media_duration_sec(tmp)
        finally:
            tmp.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        log.debug("参考视频时长实测失败（%s…）：%s", url[:60], e)
        return 0.0


def relay_durations(jobs: list[Any]) -> dict[str, float]:
    """接力锚溯源：video_url → 产生它的任务的计划时长（没有任务的直链不进映射）。"""
    out: dict[str, float] = {}
    for j in jobs or []:
        url = getattr(j, "video_url", None)
        dur = float(((getattr(j, "params", None) or {}).get("duration_sec")) or 0)
        if url and dur > 0:
            out[str(url)] = dur
    return out


def reference_usage(
    params: dict[str, Any],
    *,
    store: Any = None,
    url_durations: dict[str, float] | None = None,
) -> dict[str, int | float | None]:
    """从落库 params 数参考用量：参考视频条数/总时长、参考图张数。

    时长来源按可靠度：本机素材 ffprobe 实测 > 接力锚溯源（url_durations）
    > 只计数不计时长——宁可少报不编数。
    """
    refs = [u for u in (params.get("reference_videos") or []) if u]
    known = dict(url_durations or {})
    total = 0.0
    known_any = False
    for u in refs:
        if u.startswith(_REPLAY_PREFIX) and store is not None:
            dur = _asset_duration(store, u)
            if dur > 0:
                total += dur
                known_any = True
        elif u in known:
            total += known[u]
            known_any = True
    return {
        "ref_video_count": len(refs),
        "ref_video_sec": round(total, 1) if known_any else None,
        "ref_image_count": len([u for u in (params.get("reference_images") or []) if u]),
    }


def job_usage(
    params: dict[str, Any],
    *,
    store: Any = None,
    url_durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """一次提交的完整用量摘要（落库进 params["usage"]，序列化层直接透传）。"""
    usage = reference_usage(params, store=store, url_durations=url_durations)
    return {
        "gen_sec": round(float(params.get("duration_sec") or 0), 2),
        **usage,
    }


def fallback_usage(params: dict[str, Any]) -> dict[str, Any]:
    """老任务没有 usage 字段时的兜底：只计数不计时长（不编数、不探测）。"""
    return {
        "gen_sec": round(float(params.get("duration_sec") or 0), 2) or None,
        "ref_video_count": len([u for u in (params.get("reference_videos") or []) if u]),
        "ref_video_sec": None,
        "ref_image_count": len([u for u in (params.get("reference_images") or []) if u]),
    }
