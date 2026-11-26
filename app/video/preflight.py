# =====================================================================
# preflight.py —— 出片前体检（2026-11-26）
#
# 为什么要有这一层：剧本/导演/摄影/组折叠/批次每层都是一次独立模型输出，
# 确定性闸门只拦「它认识的那几类」。从单集 22 镜放大到 4 集 72 镜后，
# 组单元超厂商上限、OS 丢失、地点别名、场标题重复这些新量级的问题
# 才冒出来，而且每次冒的都不一样。这里把已知的几类做成每次必查：
#   - blocker：生成单元超过厂商单次上限（提交会被截断，台词断句）；
#   - warn：OS 没进提示词、场标题重复、地点未使用、集数与提案不一致、
#           提示词超预算 / 嵌套引号 / 同句重复。
# N2D 在批准前打印；generate_batch 在提交前拦 blocker（fail-closed）。
# 只做结构检查，不调模型、不花钱。
# =====================================================================

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

# 单分镜提示词预算（与 dramaturgy 的审阅口径一致；组提示词按机位数放宽）。
PROMPT_BUDGET = 250.0
DEFAULT_MAX_DURATION = 15.0


@dataclass(frozen=True)
class PlanIssue:
    level: str          # blocker | warn
    code: str
    message: str


def _norm(text: Any) -> str:
    return re.sub(r"[\s，。！？!?…；;：:\"'“”「」『』]+", "", str(text or ""))


def _prompt_of(shot: dict[str, Any]) -> str:
    return str(shot.get("group_prompt") or shot.get("video_prompt") or "")


def plan_issues(
    script: Any,
    shots: list[Any],
    *,
    max_duration: float = DEFAULT_MAX_DURATION,
    units: list[dict[str, Any]] | None = None,
) -> list[PlanIssue]:
    """检查一版镜头方案能不能进入出片。返回按严重度排序的问题列表。"""
    from .service import generation_units

    rows = [s for s in shots if isinstance(s, dict) and s.get("id")]
    issues: list[PlanIssue] = []

    # ---- 1) 生成单元时长：超过厂商上限就是硬伤 ----
    unit_rows = units if units is not None else generation_units(rows)
    over = [u for u in unit_rows if float(u.get("duration_sec") or 0) > max_duration + 0.01]
    for u in over:
        issues.append(PlanIssue(
            "blocker", "unit_over_max",
            f"{u['shot_id']} 生成单元 {float(u['duration_sec']):.1f} 秒 > 上限 "
            f"{max_duration:.0f} 秒；提交会被截断，组内台词会断在半句。"
            f"需要拆组或压时长（成员：{'、'.join(u.get('member_ids') or [])}）",
        ))

    # ---- 2) OS 内心独白是否进入提示词 ----
    all_prompts = _norm("\n".join(_prompt_of(s) for s in rows))
    missing_os: list[str] = []
    for sc in getattr(script, "scenes", []) or []:
        for b in getattr(sc, "beats", []) or []:
            if getattr(b, "type", "") == "os" and (getattr(b, "line", "") or "").strip():
                if _norm(b.line) not in all_prompts:
                    missing_os.append(f"{sc.id}「{str(b.line).strip()[:16]}」")
    if missing_os:
        issues.append(PlanIssue(
            "warn", "os_missing",
            f"{len(missing_os)} 条内心独白（OS）没有进任何提示词："
            + "、".join(missing_os[:4])
            + "；native 成片不会出现这些旁白。",
        ))

    # ---- 3) 场标题 / 地点 / 集数 ----
    titles = Counter(str(sc.title or "") for sc in (getattr(script, "scenes", []) or []))
    dup_titles = [t for t, n in titles.items() if t and n > 1]
    if dup_titles:
        issues.append(PlanIssue(
            "warn", "duplicate_scene_title",
            f"{len(dup_titles)} 个场标题重复（如「{dup_titles[0]}」×{titles[dup_titles[0]]}）："
            "工作台与导出会看起来像场号坏了。",
        ))

    used_locs = {str(getattr(sc, "location_id", "")) for sc in (getattr(script, "scenes", []) or [])}
    unused = [str(getattr(loc, "name", "")) for loc in (getattr(script, "locations", []) or [])
              if str(getattr(loc, "id", "")) not in used_locs]
    if unused:
        issues.append(PlanIssue(
            "warn", "unused_location",
            f"{len(unused)} 个地点没有任何场使用：{'、'.join(unused[:4])}；"
            "仍会生成定妆图，属于白花钱。",
        ))

    plan = getattr(script, "adaptation_plan", None)
    if plan is not None and units is None:
        ep_ids = {str(s.get("episode_id") or "") for s in rows if s.get("episode_id")}
        want = int(getattr(plan, "episode_count", 0) or 0)
        if want and len(ep_ids) != want:
            issues.append(PlanIssue(
                "warn", "episode_count_mismatch",
                f"镜头方案是 {len(ep_ids)} 集，分集提案是 {want} 集："
                "多拆/少拆会让分集总览和确认过的大纲对不上。",
            ))

    # ---- 4) 提示词质量（只报数，不逐条刷屏）----
    too_long = sum(1 for s in rows if len(_prompt_of(s)) > PROMPT_BUDGET)
    nested = [s["id"] for s in rows if "「「" in _prompt_of(s) or "」」" in _prompt_of(s)]
    # 复读只查单镜 video_prompt：组提示词会跨机位逐字重述环境/人物锚
    # （lock line），那是防漂移的设计性重复，不是病。
    repeated = [s["id"] for s in rows
                if _repeated_clause(str(s.get("video_prompt") or ""))]
    if too_long:
        issues.append(PlanIssue(
            "warn", "prompt_over_budget",
            f"{too_long}/{len(rows)} 条提示词超过 {PROMPT_BUDGET:.0f} 字预算："
            "锚墙重述会稀释尾部的台词与动作。",
        ))
    if nested:
        issues.append(PlanIssue(
            "warn", "nested_quote",
            f"{len(nested)} 条提示词有嵌套引号（「「…」」）：台词会被渲染成两层引用。",
        ))
    if repeated:
        issues.append(PlanIssue(
            "warn", "repeated_clause",
            f"{len(repeated)} 条提示词有同一句写了两遍：模型会当成两个连续动作。",
        ))

    # blocker 在前，同级按 code 稳定排序
    issues.sort(key=lambda i: (0 if i.level == "blocker" else 1, i.code))
    return issues


def _repeated_clause(text: str, *, min_len: int = 8) -> str:
    """同一段提示词里重复出现的长句（跳过引号内的台词）。"""
    parts = [p for p in re.split(r"[。！？!?；;]", str(text or "")) if p.strip()]
    seen: set[str] = set()
    for part in parts:
        if "「" in part:
            continue
        key = _norm(part)
        if len(key) >= min_len:
            if key in seen:
                return part.strip()[:24]
            seen.add(key)
    return ""
