# =====================================================================
# adaptation.py —— 段落级改编决策（D-90）
#
# 解决什么
# --------
# 旧链路拿到原文直接拆场：剧本层没有节拍预算，导演层只有「每场保底 24 秒」
# 的下限式预算。337 字《劳山道士》因此跑出 4 场 / 22 拍 / 27 镜 / 108.5 秒，
# 而它的台词估算只有约 68 秒——拖沓不是镜头拆得不好，是**改编层没有取舍**。
#
# 做法
# ----
#   1. 确定性切段（300–450 字/段，短段合并、长段按句切，保证全覆盖）；
#   2. 一次结构化 LLM 调用，逐段给出重要性 / 功能 / 人物焦点 / 处理方式 /
#      目标节拍 / 目标秒数 / 完整性风险；
#   3. 确定性纠偏（must_keep 不得删、cut 只允许 optional、目标秒数夹进
#      55–95 秒/集、expand 必须有戏剧理由），失败回退旧逻辑；
#   4. 场景规划时把段落到场，算出每场目标秒数，交给导演按目标带拆镜。
#
# 这个模块的输出只给流水线用，不进 UI（用户 2026-11-25 明确「只在后台用」）。
# =====================================================================

from __future__ import annotations

import logging
import math
import re
from typing import Any

from ..domain import AdaptationPlan, SourceSegmentDecision
from ..video.rhythm import EPISODE_MAX_SEC, EPISODE_MIN_SEC, EPISODE_TARGET_SEC

log = logging.getLogger(__name__)

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")
_SCENE_HEADER = re.compile(
    r"(?m)^\s*(第\s*[0-9０-９一二三四五六七八九十百千零两]+\s*场|内景|外景|INT\.|EXT\.)"
)
_EXPAND_FUNCTIONS = {"turn", "reveal", "climax", "character", "spectacle"}
_TREATMENTS = {"keep", "adapt", "expand", "compress", "cut"}


# ---------------------------------------------------------------------
# 一、切段
# ---------------------------------------------------------------------

def _anchor(text: str) -> str:
    """原文里最短的可核对锚（给模型对照用，不追求语义）。"""
    s = re.sub(r"^[\s　\"'「『（(\[]+", "", text or "")
    s = re.sub(r"[\s　]+", "", s)
    return s[:20]


def split_source(
    raw_text: str,
    *,
    max_chars: int = 450,
    min_chars: int = 40,
    max_segments: int = 24,
) -> list[dict[str, Any]]:
    """把原文切成 300–450 字的段，返回 [{id,text,chars,anchor}]。

    先按空行切段（中文小说常一段一行，空行切不出来时退回单行），
    长段按句号/问号/分号再切，短段向后合并；段数超过上限时按目标长度
    二次合并，保证一次请求能装下。
    """
    text = (raw_text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) <= 1:
        paras = [p.strip() for p in text.split("\n") if p.strip()]

    chunks: list[str] = []
    for para in paras:
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        buf = ""
        for sent in _SENT_SPLIT.split(para):
            if not sent:
                continue
            if buf and len(buf) + len(sent) > max_chars:
                chunks.append(buf)
                buf = sent
            else:
                buf += sent
        if buf:
            chunks.append(buf)

    merged: list[str] = []
    for c in chunks:
        if merged and len(merged[-1]) < min_chars and len(merged[-1]) + len(c) <= max_chars:
            merged[-1] += c
        else:
            merged.append(c)

    if len(merged) > max_segments:
        target = max(1, math.ceil(sum(len(c) for c in merged) / max_segments))
        grouped: list[str] = []
        buf = ""
        for c in merged:
            if buf and len(buf) + len(c) > target:
                grouped.append(buf)
                buf = c
            else:
                buf += c
        if buf:
            grouped.append(buf)
        merged = grouped

    return [
        {"id": f"seg_{i:03d}", "text": c, "chars": len(c), "anchor": _anchor(c)}
        for i, c in enumerate(merged, 1)
    ]


def is_narrative_source(raw_text: str) -> bool:
    """已经是剧本格式（有场次/内景外景抬头）的原文不做段落改编分析。"""
    text = raw_text or ""
    return len(_SCENE_HEADER.findall(text)) < 2


# ---------------------------------------------------------------------
# 二、LLM 分析
# ---------------------------------------------------------------------

def _system_prompt() -> str:
    from ..skills import load_skill

    body = load_skill("adaptation-plan").body
    return (
        body
        + "\n\n【本次输出】只返回一个 JSON 对象；segments 必须覆盖用户给出的每一个 seg id，"
          "不要新增、不要漏。scene_targets / scene_segments 留空，由代码在场景规划后填。"
    )


def _user_prompt(
    title: str,
    segments: list[dict[str, Any]],
    *,
    target_hint: float | None = None,
    notes: str = "",
) -> str:
    total = sum(int(s["chars"]) for s in segments)
    lines = [
        f"标题：{title}",
        f"原文共约 {total} 字，已切成 {len(segments)} 段：",
        "",
    ]
    for seg in segments:
        lines.append(f"[{seg['id']}｜{seg['chars']} 字｜锚：{seg['anchor']}]")
        lines.append(str(seg["text"]))
        lines.append("")
    if target_hint:
        lines.append(f"用户/项目给定的目标时长：约 {target_hint:.0f} 秒。")
    if notes.strip():
        lines.append(f"用户额外要求：{notes.strip()}")
    lines.append(
        "请逐段决策。短剧每集 55–95 秒，默认 75；先判断原文事件量，再决定"
        "哪些段扩写、哪些压缩、哪些删除。must_keep 不得 cut。"
    )
    return "\n".join(lines)


def _call(llm: Any, title: str, segments: list[dict[str, Any]], *, target_hint: float | None,
          notes: str, critique: str = "") -> AdaptationPlan | None:
    user = _user_prompt(title, segments, target_hint=target_hint, notes=notes)
    if critique:
        user += f"\n\n上一版的问题（请修正）：\n{critique}"
    try:
        resp = llm.structured(AdaptationPlan).invoke([
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": user},
        ])
        plan = resp if isinstance(resp, AdaptationPlan) else AdaptationPlan.model_validate(resp)
    except Exception as e:  # noqa: BLE001
        log.warning("段落决策调用失败：%s", str(e)[:200])
        return None
    return plan


def _fill_missing(plan: AdaptationPlan, segments: list[dict[str, Any]]) -> list[str]:
    """把漏掉的段补成 keep，把不存在的段丢掉。返回人话说明。"""
    notes: list[str] = []
    by_id: dict[str, SourceSegmentDecision] = {}
    for seg in plan.segments:
        if seg.id not in by_id:
            by_id[seg.id] = seg
    valid_ids = {s["id"] for s in segments}
    dropped = [sid for sid in by_id if sid not in valid_ids]
    if dropped:
        notes.append(f"丢弃模型编出的段落 id：{', '.join(dropped[:5])}")
        for sid in dropped:
            by_id.pop(sid, None)

    missing = [s for s in segments if s["id"] not in by_id]
    if missing:
        notes.append(f"补 {len(missing)} 个漏决策段落（默认保留/1 拍）")
    for seg in missing:
        by_id[seg["id"]] = SourceSegmentDecision(
            id=seg["id"],
            summary=str(seg["text"])[:32],
            source_anchor=seg["anchor"],
            plot_importance="important",
            function="setup",
            treatment="keep",
            target_beats=1,
            target_sec=0.0,
            reason="模型未给出决策，按保留处理",
        )
    order = {s["id"]: i for i, s in enumerate(segments)}
    plan.segments = sorted(by_id.values(), key=lambda s: order.get(s.id, 10**6))
    return notes


def _repair(plan: AdaptationPlan, segments: list[dict[str, Any]]) -> list[str]:
    """确定性纠偏。返回说明（含错误/建议），不改原文、不删 must_keep。"""
    notes = _fill_missing(plan, segments)

    for seg in plan.segments:
        if seg.treatment not in _TREATMENTS:
            seg.treatment = "keep"
        if seg.plot_importance == "must_keep" and seg.treatment == "cut":
            seg.treatment = "compress"
            seg.reason = (seg.reason + "；" if seg.reason else "") + "不可删段落，已从删除改为压缩"
            notes.append(f"{seg.id} 是不可删段落，已改为压缩")
        if seg.treatment == "cut":
            risky = bool((seg.completeness_risk or "").strip())
            if seg.plot_importance != "optional" or risky:
                seg.treatment = "compress"
                seg.reason = (seg.reason + "；" if seg.reason else "") + "删除有完整性风险，已改为压缩"
                notes.append(f"{seg.id} 有完整性风险，已从删除改为压缩")
        if seg.treatment == "expand":
            justified = seg.function in _EXPAND_FUNCTIONS or bool((seg.character_focus or "").strip())
            if not justified:
                seg.treatment = "keep"
                notes.append(f"{seg.id} 没有戏剧理由，扩写已降级为保留")
        if seg.treatment == "cut":
            seg.target_beats = 0
            seg.target_sec = 0.0
        else:
            seg.target_beats = max(1, min(6, int(seg.target_beats or 1)))
            # 一段的秒数要和它的拍数对得上：2.5–5 秒/拍（短剧切镜密度）。
            # 否则模型会写「4 拍 / 30 秒」，导演照着目标拆时只能注水。
            seg.target_sec = max(
                seg.target_beats * 2.5,
                min(seg.target_beats * 5.0, float(seg.target_sec or 0.0)),
            )

    # 目标秒数：先合计，再按集数夹进 55–95/集
    active = [s for s in plan.segments if s.treatment != "cut"]
    total = sum(s.target_sec for s in active)
    if total <= 0:
        beats = sum(s.target_beats for s in active) or len(active) or 1
        for s in active:
            s.target_sec = round(EPISODE_TARGET_SEC * (s.target_beats / beats), 1)
        total = sum(s.target_sec for s in active)
        notes.append(f"段落目标秒数缺失，按节拍比例分配 {total:.0f} 秒")
    else:
        zero = [s for s in active if s.target_sec <= 0]
        if zero:
            per = max(2.0, total / max(1, len(active)))
            for s in zero:
                s.target_sec = round(per, 1)
            total = sum(s.target_sec for s in active)
            notes.append(f"{len(zero)} 段没有目标秒数，按均值补 {per:.0f} 秒")

    episodes = max(1, math.ceil(total / EPISODE_MAX_SEC))
    lo, hi = episodes * EPISODE_MIN_SEC, episodes * EPISODE_MAX_SEC
    target = min(max(total, lo), hi)
    # 微型素材（<500 字）不硬撑到 75 秒：337 字实测 27 镜 / 108 秒的病根之一
    # 就是「默认 75 秒」被当成硬目标。55–65 秒足够讲完一个完整小事件。
    source_chars = sum(int(s["chars"]) for s in segments)
    if source_chars < 500 and target > 65.0:
        notes.append(f"源文 {source_chars} 字，目标从 {target:.0f} 秒压到 65 秒（微型素材）")
        target = 65.0
        episodes = 1
        lo, hi = EPISODE_MIN_SEC, EPISODE_MAX_SEC
    if abs(target - total) > 0.5 and total > 0:
        factor = target / total
        for s in active:
            s.target_sec = round(s.target_sec * factor, 1)
        notes.append(f"目标时长 {total:.0f} 秒夹进 {lo:.0f}–{hi:.0f}（{episodes} 集）")
    plan.target_sec = round(sum(s.target_sec for s in active), 1)
    plan.episode_count = max(1, math.ceil(plan.target_sec / EPISODE_MAX_SEC)) if plan.target_sec else episodes

    if not (plan.hook or "").strip():
        hook = next((s for s in plan.segments if s.function in ("hook", "turn", "climax")), None)
        plan.hook = (hook.summary or hook.source_anchor) if hook else (
            plan.segments[0].summary if plan.segments else "")
        if plan.hook:
            notes.append("开场钩子缺失，用第一个转折/高潮段兜底")

    if plan.segments and all(s.treatment == "keep" for s in plan.segments):
        notes.append("建议：全部 keep——如果原文没有取舍，说明分析没做功")
    if not any(s.treatment == "compress" for s in plan.segments) and len(plan.segments) >= 4:
        notes.append("建议：没有压缩段——检查是否有过场/重复信息没被压")
    return notes


def analyze_adaptation(
    llm: Any,
    *,
    title: str,
    raw_text: str,
    adaptation_type: str = "short_drama",
    language: str = "zh-CN",
    target_hint: float | None = None,
    notes: str = "",
    max_retries: int = 1,
    segments: list[dict[str, Any]] | None = None,
) -> AdaptationPlan | None:
    """逐段改编分析。模型不可用/连续失败返回 None（调用方回退旧逻辑）。"""
    if not getattr(llm, "available", False):
        return None
    segs = segments if segments is not None else split_source(raw_text)
    if not segs:
        return None

    critique = ""
    plan: AdaptationPlan | None = None
    for attempt in range(max_retries + 1):
        plan = _call(llm, title, segs, target_hint=target_hint, notes=notes, critique=critique)
        if plan is None:
            critique = "上一次没有返回可解析的 JSON。请严格按 schema 返回。"
            continue
        plan.scene_targets = {}
        plan.scene_segments = {}
        notes_out = _repair(plan, segs)
        all_keep = bool(plan.segments) and all(s.treatment == "keep" for s in plan.segments)
        lazy = all_keep and len(segs) >= 4 and attempt < max_retries
        if lazy:
            critique = ("上一版全部标了 keep，等于没有做取舍。请至少标出 1 段 compress、"
                        "1 段 expand 或 1 段 cut，并写清理由（源文确实没有可取舍处才全 keep）。")
            continue
        for n in notes_out:
            log.info("段落决策：%s", n)
        return plan
    return None


# ---------------------------------------------------------------------
# 三、给场景规划/导演的 brief 与校验
# ---------------------------------------------------------------------

def plan_brief(plan: AdaptationPlan) -> str:
    """拼进 _stage_scenes 的段落决策块（紧凑，不重复整段原文）。"""
    total_beats = sum(s.target_beats for s in plan.segments if s.treatment != "cut")
    max_beats = max(1, int(total_beats * 1.3))
    lines = [
        "【本片段落改编决策（内部，不展示给用户）】",
        f"总目标 {plan.target_sec:.0f} 秒 / {plan.episode_count} 集；"
        f"总计约 {total_beats} 拍（全片最多 {max_beats} 拍，每场 2–5 拍，不要注水）；"
        f"开场钩子：{plan.hook or '（未给）'}",
        "逐段处理（每段都要在场景的 source_segments 里认领，不得漏段）：",
    ]
    for seg in plan.segments:
        tag = f"{seg.id} [{seg.plot_importance}｜{seg.treatment}"
        if seg.character_focus:
            tag += f"｜人物：{seg.character_focus}"
        tag += f"｜{seg.target_beats} 拍/{seg.target_sec:.0f}s]"
        text = (seg.summary or seg.source_anchor or "").strip()
        reason = (seg.reason or "").strip()
        line = f"- {tag} {text}"
        if reason:
            line += f" —— {reason}"
        lines.append(line)
    lines += [
        "【硬规则】",
        "1) 每场必须写 source_segments（覆盖的 seg id）；每场 target_sec = 所覆盖段 target_sec 之和（±10%）。",
        "2) 节拍数按 treatment 控制：expand 可多 1–2 拍，compress 少 1–2 拍，cut 的段不许写进剧本。",
        "3) must_keep 段必须落场且有节拍；cut 只用于 optional 且 completeness_risk 为空的段。",
        "4) 扩写只加人物反应/阻碍/决定/可拍细节，不新增副线、不新增人物、不改变事件因果。",
        "5) 场数按素材量：几百字 3–5 场，常规 5–7 场；不为纯过场单独开场。",
        f"6) 第一个场景的第一个节拍必须落在开场钩子上：{plan.hook or '用一个强画面冷开场'}；"
        "不要用环境介绍/日常铺垫开场。",
    ]
    return "\n".join(lines)


def _segment_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in re.split(r"[,，、\s]+", value) if x.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def scene_plan_issues(plan: AdaptationPlan, scene_plan: Any) -> list[str]:
    """场景规划（ScenePlan）与段落决策的一致性检查；错误会触发一次重拆。"""
    if plan is None or scene_plan is None:
        return []
    source_ids = {s.id for s in plan.segments}
    cut_ids = {s.id for s in plan.segments if s.treatment == "cut"}
    must_ids = {s.id for s in plan.segments if s.plot_importance == "must_keep"}

    assigned: dict[str, str] = {}
    issues: list[str] = []
    scene_target_sum = 0.0
    for sc in scene_plan.scenes:
        sid = str(getattr(sc, "scene_id", "") or getattr(sc, "id", "") or "")
        ids = _segment_ids(getattr(sc, "source_segments", None))
        if not ids:
            issues.append(f"错误：{sid} 没有 source_segments（必须认领覆盖的 seg id）")
        for seg_id in ids:
            if seg_id not in source_ids:
                issues.append(f"错误：{sid} 引用了不存在的段落 {seg_id}")
            elif seg_id in cut_ids:
                issues.append(f"错误：{sid} 引用了已删除段落 {seg_id}（cut 段不许写进剧本）")
            elif seg_id in assigned:
                issues.append(f"错误：段落 {seg_id} 同时被 {assigned[seg_id]} 和 {sid} 认领")
            else:
                assigned[seg_id] = sid
        target = getattr(sc, "target_sec", None)
        if target is not None:
            scene_target_sum += float(target or 0)

    missing = sorted(must_ids - set(assigned))
    if missing:
        issues.append("错误：不可删段落没有落场：" + "、".join(missing))

    # 节拍量：太少=漏拍/太赶，太多=注水；用段落决策的 target_beats 当尺子。
    actual_beats = sum(len(getattr(sc, "beats", []) or []) for sc in scene_plan.scenes)
    expected_beats = sum(s.target_beats for s in plan.segments if s.treatment != "cut")
    if expected_beats:
        if actual_beats < expected_beats * 0.7:
            issues.append(
                f"错误：剧本只有 {actual_beats} 拍，段落决策要求约 {expected_beats} 拍（漏拍/太赶）")
        elif actual_beats > expected_beats * 1.5:
            issues.append(
                f"错误：剧本写了 {actual_beats} 拍，段落决策只要约 {expected_beats} 拍（注水）")
    if plan.target_sec > 0 and scene_target_sum > 0:
        lo, hi = plan.target_sec * 0.85, plan.target_sec * 1.15
        if not (lo <= scene_target_sum <= hi):
            issues.append(
                f"错误：各场 target_sec 合计 {scene_target_sum:.0f}s，与段落目标 "
                f"{plan.target_sec:.0f}s 相差超过 15%"
            )
    # 时长闸门：target_sec 合计可能故意写小，但正文实际念白+动作仍会超。
    if plan.target_sec > 0:
        from ..video.rhythm import speech_need_seconds

        estimate = 0.0
        for sc in scene_plan.scenes:
            for b in getattr(sc, "beats", []) or []:
                kind = str(getattr(b, "type", "action") or "action").strip().lower()
                if kind in ("dialogue", "os"):
                    estimate += speech_need_seconds(str(getattr(b, "line", "") or ""))
                elif kind in ("action", "cue"):
                    estimate += 2.5
        hard_max = EPISODE_MAX_SEC * max(1, plan.episode_count)
        limit = min(plan.target_sec * 1.2, hard_max)
        if estimate > limit + 0.5:
            issues.append(
                f"错误：剧本估算 {estimate:.0f} 秒，超出上限 {limit:.0f} 秒"
                f"（目标 {plan.target_sec:.0f} 秒 / 短剧单集 {EPISODE_MAX_SEC:.0f} 秒）；"
                "合并重复过场/反应镜头，不要删 must_keep")
        elif estimate < plan.target_sec * 0.7:
            issues.append(
                f"错误：剧本估算 {estimate:.0f} 秒，明显低于目标 {plan.target_sec:.0f} 秒（漏拍）")
    return issues


def bind_scene_targets(plan: AdaptationPlan, scene_plan: Any, scenes: list[Any]) -> list[str]:
    """把段落落到场，算每场目标秒数；返回修复说明。

    scenes 与 scene_plan.scenes 按顺序一一对应（generation 的转换代码保证）。
    """
    notes: list[str] = []
    seg_by_id = {s.id: s for s in plan.segments}
    cut_ids = {s.id for s in plan.segments if s.treatment == "cut"}
    usable = [s.id for s in plan.segments if s.treatment != "cut"]
    if not scenes:
        return ["没有场景，无法绑定段落"]

    declared: dict[str, list[str]] = {}
    used: set[str] = set()
    for i, sc in enumerate(scene_plan.scenes):
        if i >= len(scenes):
            break
        sid = scenes[i].id
        ids: list[str] = []
        for seg_id in _segment_ids(getattr(sc, "source_segments", None)):
            if seg_id in seg_by_id and seg_id not in cut_ids and seg_id not in used:
                ids.append(seg_id)
                used.add(seg_id)
        declared[sid] = ids

    # 漏认领的段按原顺序补到「当前段数最少」的场（同分取靠前），保证 must_keep 落场
    for seg_id in usable:
        if seg_id in used:
            continue
        candidates = [scenes[i].id for i in range(min(len(scene_plan.scenes), len(scenes)))]
        if not candidates:
            candidates = [scenes[0].id]
        target = min(candidates, key=lambda sid: (len(declared.get(sid, [])), candidates.index(sid)))
        declared.setdefault(target, []).append(seg_id)
        used.add(seg_id)
        notes.append(f"段落 {seg_id} 未认领，已补到 {target}")

    # 每场原始目标 = 覆盖段的 target_sec 之和；没有段就按节拍兜底
    raw: dict[str, float] = {}
    total_beats = sum(len(getattr(sc, "beats", []) or []) for sc in scenes) or 1
    for i, scene in enumerate(scenes):
        seg_target = sum(seg_by_id[sid].target_sec for sid in declared.get(scene.id, []))
        if seg_target <= 0:
            sc_target = getattr(scene_plan.scenes[i], "target_sec", None) if i < len(scene_plan.scenes) else None
            seg_target = float(sc_target or 0)
        if seg_target <= 0:
            seg_target = plan.target_sec * len(getattr(scene, "beats", []) or []) / total_beats
        raw[scene.id] = max(0.0, seg_target)

    total = sum(raw.values())
    if total > 0 and plan.target_sec > 0:
        factor = plan.target_sec / total
        if abs(factor - 1.0) > 0.05:
            notes.append(f"各场目标按比例校准到 {plan.target_sec:.0f} 秒（原合计 {total:.0f} 秒）")
        raw = {sid: v * factor for sid, v in raw.items()}

    plan.scene_segments = {sid: ids for sid, ids in declared.items() if ids}
    plan.scene_targets = {sid: round(v, 1) for sid, v in raw.items()}
    return notes


def estimate_script_seconds(script: Any) -> float:
    """剧本的粗估时长：台词口播秒数 + 动作拍 × 2.5 秒。"""
    from ..video.rhythm import speech_need_seconds

    total = 0.0
    for sc in getattr(script, "scenes", []) or []:
        text = "".join((b.line or "") for b in sc.beats if b.type == "dialogue")
        if text.strip():
            total += speech_need_seconds(text)
        total += sum(1 for b in sc.beats if b.type == "action") * 2.5
    return total


def coverage_issues(script: Any, plan: AdaptationPlan | None) -> list[str]:
    """剧本层体检：must_keep 覆盖、cut 清理、时长带、人物焦点。"""
    if plan is None:
        return []
    issues: list[str] = []
    beats_by_scene = {sc.id: len(sc.beats) for sc in getattr(script, "scenes", []) or []}
    assigned: dict[str, list[str]] = {}
    for sid, ids in (plan.scene_segments or {}).items():
        for seg_id in ids:
            assigned.setdefault(seg_id, []).append(sid)

    for seg in plan.segments:
        if seg.plot_importance == "must_keep":
            rooms = assigned.get(seg.id) or []
            if not rooms or all(beats_by_scene.get(r, 0) == 0 for r in rooms):
                issues.append(f"错误：不可删段落 {seg.id}（{seg.summary or seg.source_anchor}）没有落场")
        if seg.treatment == "cut":
            if any(beats_by_scene.get(r, 0) > 0 for r in assigned.get(seg.id, [])):
                issues.append(f"错误：已删除段落 {seg.id} 仍被写成节拍")

    est = estimate_script_seconds(script)
    target = float(plan.target_sec or 0)
    if target > 0:
        if est > target * 1.25:
            issues.append(f"错误：剧本估算 {est:.0f} 秒，超出目标 {target:.0f} 秒的 1.25 倍")
        elif est < target * 0.7:
            issues.append(f"建议：剧本估算 {est:.0f} 秒，明显低于目标 {target:.0f} 秒（可能漏拍）")

    chars = list(getattr(script, "characters", []) or [])
    for scene in getattr(script, "scenes", []) or []:
        corpus = "".join((b.text or "") + (b.line or "") for b in scene.beats)
        for seg_id in (plan.scene_segments or {}).get(scene.id, []):
            seg = plan.segment(seg_id)
            if seg is None or not (seg.character_focus or "").strip():
                continue
            focus = seg.character_focus.strip()
            focus_name = re.split(r"[：:，,（(]", focus)[0].strip()
            hit = any(c.name and c.name in corpus for c in chars) or \
                any(c.id in corpus for c in chars)
            if focus_name:
                hit = hit or focus_name in corpus
            if not hit:
                issues.append(f"建议：{scene.id} 的段落 {seg_id} 标了人物焦点「{focus}」，但节拍里找不到对应人物")
    return issues


def adaptation_summary(plan: AdaptationPlan | None) -> str:
    """版本备注用的一行话（不展示表格）。"""
    return plan.summary_line() if plan is not None else ""
