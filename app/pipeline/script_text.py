# =====================================================================
# script_text.py —— 剧本文本层确定性体检（2026-11-26）
#
# 用户反馈：不能只看最后的视频提示词，中间生成的剧本正文也要把关。
# 参考短剧工作台的格式后，这里检查的是「文本本身能不能用」：
#   - 场头：集号 / 内外景 / 时间是否齐；
#   - 正文：有没有英文残留、文言残留、不可拍的心理/听觉描写、重复台词；
#   - 人物：场景 characters 是否漏了动作里出现的人；
#   - 卡点：整片有没有 card（钩子/卡点）节拍。
#
# 纯字符串运算，不调 LLM；生成后跑一遍，问题进 artifacts / 剧本医生。
# =====================================================================

from __future__ import annotations

import re

from ..domain import Script, ScriptBeat
from .source_style import classical_score, strict_classical_score

_ASCII_RE = re.compile(r"[A-Za-z]{2,}")
_UNFILMABLE_RE = re.compile(
    r"心想|心里想|心中想|意识到|感到|感觉到|决定|回忆起|想起|明白过来|"
    r"知道了|发现|声音软下来|语气软下来|心里一沉|心头一紧"
)
_DIALOGUE_NORM_RE = re.compile(r"[，。！？!?…；;：:\s「」『』“”\"']+")
# 引号里的原唱歌词/原句可以保留，不当作叙述文言。
_QUOTED_RE = re.compile(r"[「『“\"].*?[」』”\"]")


def _ascii_hits(text: str) -> list[str]:
    """英文残留：允许 AI/CEO/APP 这类常见大写缩写，抓 paper/beta 这种。"""
    out: list[str] = []
    for m in _ASCII_RE.finditer(text or ""):
        tok = m.group(0)
        if tok.isupper() and len(tok) <= 5:
            continue
        out.append(tok)
    return out


def ensure_hook_card(script: Script, plan: object | None = None) -> int:
    """整片没有 card 时，在首场开头补一个「开场卡点」。

    模型经常漏卡点（v7 全片 0 个 card）。这里用段落决策的 hook / logline
    补一个文本标记，不动剧情、不占台词；返回补了几个。
    """
    if any(b.type == "card" for sc in script.scenes for b in sc.beats):
        return 0
    if not script.scenes:
        return 0
    hook = str(getattr(plan, "hook", "") or "").strip() or str(script.logline or "").strip()
    if not hook:
        return 0
    first = script.scenes[0]
    used = {b.id for b in first.beats}
    n = 0
    while f"beat_{n:03d}" in used:
        n += 1
    first.beats.insert(0, ScriptBeat(
        id=f"beat_{n:03d}", type="card", text=f"开场卡点：{hook}"))
    return 1


def audit_script_text(script: Script) -> list[str]:
    """返回人话问题列表；空列表 = 文本层没查出硬伤。"""
    issues: list[str] = []
    style = str(getattr(script, "dialogue_style", "") or "")

    # ---- 人物小传：英文残留 / 文言原句 ----
    for c in script.characters:
        blob = " ".join(
            str(x or "") for x in (
                c.name, c.description, c.goal, c.motivation, c.personality,
                c.visual, c.tags, c.background, c.history, c.relationships, c.arc,
            )
        )
        for tok in _ascii_hits(blob):
            issues.append(f"人物小传 {c.name} 里有英文「{tok}」")
        if style == "vernacular":
            unquoted = _QUOTED_RE.sub("", blob)
            strict = strict_classical_score(unquoted)
            modern = classical_score(unquoted)[1]
            if strict >= 3 and strict > modern:
                issues.append(
                    f"人物小传 {c.name} 里仍有文言原句（之乎者也/曰），和「全部白话」不一致")

    seen_lines: dict[str, str] = {}
    has_card = False
    for sc in script.scenes:
        # ---- 场头 ----
        if not (sc.time or "").strip():
            issues.append(f"{sc.id} 场头没有时间（日/夜/晨/傍晚）")
        if sc.interior_exterior not in ("内景", "外景"):
            issues.append(f"{sc.id} 场头没有标内景/外景")
        if int(sc.episode or 0) < 1:
            issues.append(f"{sc.id} 没有集号")

        # 只扫动作/画面：对白里「老道士骗我」不代表道士在本场。
        corpus = " ".join(
            [str(x) for x in (sc.action or [])]
            + [(b.text or "") for b in sc.beats if b.type in ("action", "cue")]
        )
        # ---- 出场人物漏人 ----
        for c in script.characters:
            if c.name and c.name in corpus and c.id not in sc.characters:
                issues.append(
                    f"{sc.id} 动作里提到 {c.name}，但 characters 没列（选角与人物锚会漏）")

        for b in sc.beats:
            if b.type == "card":
                has_card = True
            text = (b.line if b.type in ("dialogue", "os") else b.text) or ""
            for tok in _ascii_hits(text):
                issues.append(f"{sc.id} {b.id} 有英文残留「{tok}」")
            if b.type == "dialogue" and style == "vernacular":
                unquoted = _QUOTED_RE.sub("", text)
                strict = strict_classical_score(unquoted)
                modern = classical_score(unquoted)[1]
                if strict >= 2 and strict > modern:
                    issues.append(f"{sc.id} {b.id} 台词仍有文言残留：{text[:30]}")
            if b.type in ("action", "cue") and _UNFILMABLE_RE.search(text):
                issues.append(
                    f"{sc.id} {b.id} 动作里有心理/听觉描写，拍不出来：{text[:30]}")
            if b.type == "dialogue" and text.strip():
                key = _DIALOGUE_NORM_RE.sub("", text)
                if len(key) >= 6 and key in seen_lines:
                    issues.append(f"{sc.id} {b.id} 与 {seen_lines[key]} 台词重复")
                else:
                    seen_lines.setdefault(key, b.id)

    if not has_card and any(sc.episode for sc in script.scenes):
        issues.append("整片没有 card 节拍（开场卡点/结尾钩子）——短剧节奏会散")

    target = float(getattr(getattr(script, "adaptation_plan", None), "target_sec", 0) or 0)
    est = 0.0
    if target:
        from .adaptation import estimate_script_seconds

        est = estimate_script_seconds(script)
        if est > target * 1.25:
            issues.append(f"剧本估算 {est:.0f} 秒，超出目标 {target:.0f} 秒的 1.25 倍（注水）")
        elif est < target * 0.7:
            issues.append(f"剧本估算 {est:.0f} 秒，明显低于目标 {target:.0f} 秒（漏拍）")
    # 短剧单集硬上限 95 秒：target*1.25 再宽也不能突破它。
    if (script.adaptation is not None
            and script.adaptation.type == "short_drama" and est > 0):
        from ..video.rhythm import EPISODE_MAX_SEC

        episodes = max(1, max((sc.episode or 1) for sc in script.scenes))
        if round(est) > EPISODE_MAX_SEC * episodes:
            issues.append(
                f"剧本估算 {est:.0f} 秒，超过短剧单集上限 {EPISODE_MAX_SEC:.0f} 秒")
    return issues
