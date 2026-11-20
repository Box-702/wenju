# =====================================================================
# repair.py —— 台词一致性修复（确定性，零 LLM）
#
# 解决什么（2026-11-20 对标即梦实操记录，C2）：
# 即梦记录里最狠的一条能力是「生成视频台词和剧本对不上 → 定位根因 →
# 按原文精确替换 → 只重出受影响片段」。我们的链路同样会分叉：
#   · DP 的 prompt_body 是 LLM 写的，它可能把台词**意译**成另一句
#     （「不出一月，定然还清」写成「一个月肯定还你」）；
#   · 也可能整段漏写台词（靠 _insert_dialogue 兜底补，但兜底只补
#     dialogue_ref 指向的那一句，组内多句会漏）；
#   · 人工在编辑器改过提示词后，台词与剧本节拍对不上。
# 台词是成片人声与字幕的唯一来源（native 模式），分叉 = 「有字幕没声音」
# 「配音念的不是这句」——A2/A4 修了位置与检测，这里修**内容**。
#
# 三件事：
#   find_divergences()  逐镜比对「剧本该说的」vs「提示词实际写的」
#   repair_dialogue()   把分叉的台词改回原文（逐字），打回待审
#   replace_text()      带上下文的精确替换（即梦同款：唯一匹配才动）
#
# 口径与 DP/字幕层一致：官方句式 `说话人说话：「台词」`（冒号挂引号），
# 招牌引用（写下「…」，无冒号）不算台词。语言按提示词本身判（中英）。
# =====================================================================

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..domain import Script, assign_dialogue_ranges

# 官方音画台词句式（与 dp._OFFICIAL_DLG_RE / subtitles.timeline_from_prompts 同源）：
# 说话动词挂冒号 + 引号包台词。中英两套动词表。
# 说话人字符集**不含句读**——否则「掌柜的手提笔。掌柜说话」会把前句整个
# 吞成说话人，替换时连动作描写一起毁掉（2026-11-20 测试实测）。
_SPEECH_VERBS = "说话|说道|开口说|低声说|轻声说|念|喊|叫道|问道|回答|说"
_DLG_RE_ZH = re.compile(
    rf"(?P<speaker>[\u4e00-\u9fffA-Za-z0-9·@()（）\-]{{0,24}})"
    rf"(?P<verb>{_SPEECH_VERBS})\s*[:：]\s*[「『“\"](?P<text>[^」』”\"]+)[」』”\"]"
)
# 英文句式：`Name says: "line"` / `Name (whispers): "line"`。
# 说话人字符集不含句号——否则 "A quiet room. He whispers" 会把整句前文
# 吞成说话人（2026-11-20 测试实测）。
_DLG_RE_EN = re.compile(
    r"(?P<speaker>[A-Za-z][A-Za-z0-9 _-]{0,40}?)\s*"
    r"(?:says|speaks|whispers|asks|replies|mutters|shouts)\s*[:：]\s*"
    r"[\"“](?P<text>[^\"”]+)[\"”]",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    """台词比对归一：去空白与中英标点，只留字。"""
    return re.sub(r"[\s，。！？、；：…—－「」『』“”\"'（）()\-~,!?.;]", "", text or "")


@dataclass
class Divergence:
    """一个镜头的台词分叉。kind：missing（该说没写）/ wrong（写岔了）/ extra（多写）。"""

    shot_id: str
    scene_id: str
    kind: str
    expected: str = ""          # 剧本节拍原文（missing/wrong 时）
    found: str = ""             # 提示词里实际写的（wrong/extra 时）
    speaker: str = ""
    detail: str = ""


@dataclass
class RepairReport:
    """一次修复的总账。"""

    checked: int = 0
    divergences: list[Divergence] = field(default_factory=list)
    repaired_shots: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.divergences)


def prompt_has_english(prompt: str) -> bool:
    """提示词正文以英文为主（台词句式按英文判）。粗判：拉丁字母远多于汉字。"""
    latin = len(re.findall(r"[A-Za-z]", prompt or ""))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", prompt or ""))
    return latin > cjk * 2 and latin > 20


def extract_prompt_dialogue(prompt: str) -> list[tuple[str, str]]:
    """提示词里逐条官方句式台词：[(说话人, 台词), ...]（按出现顺序）。"""
    rx = _DLG_RE_EN if prompt_has_english(prompt) else _DLG_RE_ZH
    out: list[tuple[str, str]] = []
    for m in rx.finditer(prompt or ""):
        text = (m.group("text") or "").strip()
        if not text:
            continue
        speaker = (m.groupdict().get("speaker") or "").strip()
        out.append((speaker, text))
    return out


def expected_dialogue(script: Script, shot_dicts: list[dict[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    """每个镜头「该说的台词」：{shot_id: [(说话人名, 台词原文), ...]}。

    权威来源 = 剧本节拍 + assign_dialogue_ranges（与配音/字幕/导演校验同一把尺）。
    组单元：台词按机位（成员）归属，组头的组提示词应覆盖全组成员的台词——
    这里逐成员列，repair 时把整组成员的台词都并进组提示词。
    """
    from ..domain import Shot

    shots = [Shot.model_validate(s) for s in shot_dicts if isinstance(s, dict) and s.get("id")]
    char_of = {c.id: c.name for c in (script.characters or [])}
    ranges = assign_dialogue_ranges(shots, [b for sc in script.scenes for b in sc.beats])
    out: dict[str, list[tuple[str, str]]] = {}
    for sid, beats in ranges.items():
        out[sid] = [(char_of.get(b.speaker or "", b.speaker or ""), (b.line or "").strip())
                    for b in beats if (b.line or "").strip()]
    return out


def _shot_unit_lines(shot_id: str, shot_dicts: list[dict[str, Any]],
                     expected: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str]]:
    """提交单元（单镜或整组）该说的全部台词：组头要并上全组成员的。"""
    head = next((s for s in shot_dicts if s.get("id") == shot_id), {})
    gid = str(head.get("group_id") or "")
    member_ids = [shot_id]
    if gid:
        member_ids = [str(s.get("id")) for s in shot_dicts
                      if isinstance(s, dict) and str(s.get("group_id") or "") == gid
                      and str(s.get("scene_id") or "") == str(head.get("scene_id") or "")]
    lines: list[tuple[str, str]] = []
    for mid in member_ids:
        lines.extend(expected.get(mid) or [])
    return lines


def find_divergences(script: Script, shot_dicts: list[dict[str, Any]]) -> RepairReport:
    """逐提交单元比对「该说的」vs「提示词实际写的」，产出分叉清单（不改动数据）。"""
    report = RepairReport()
    expected = expected_dialogue(script, shot_dicts)
    seen_units: set[str] = set()
    for s in shot_dicts:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sid = str(s["id"])
        gid = str(s.get("group_id") or "")
        # 组只查组头一次（提交的是组提示词，覆盖全组）
        unit_id = sid
        if gid:
            members = [x for x in shot_dicts if isinstance(x, dict)
                       and str(x.get("group_id") or "") == gid
                       and str(x.get("scene_id") or "") == str(s.get("scene_id") or "")]
            members.sort(key=lambda x: int(x.get("group_index") or 0))
            unit_id = str(members[0].get("id"))
            if unit_id in seen_units:
                continue
            seen_units.add(unit_id)
        head = next((x for x in shot_dicts if x.get("id") == unit_id), s)
        prompt = str(head.get("group_prompt") or head.get("video_prompt") or "")
        if not prompt:
            continue
        report.checked += 1
        want = _shot_unit_lines(unit_id, shot_dicts, expected)
        got = extract_prompt_dialogue(prompt)
        got_norm = [_norm(t) for _, t in got]
        # 逐条对齐：DP 按节拍顺序写台词，所以「该说的」与「写了的」按顺序配对。
        # 每条期望台词：先找逐字命中（任意位置）→ 干净；否则有未配对的
        # 实际台词 → 判意译（wrong，found=那条）；没有 → 判漏写（missing）。
        # 意译的措辞与原文往往毫无公共子串（「不出一月，定然还清」vs
        # 「一个月肯定还你」），靠包含关系永远判不出来——顺序配对才抓得住。
        matched_got: set[int] = set()
        for spk, line in want:
            wn = _norm(line)
            if not wn:
                continue
            exact = next((j for j, gn in enumerate(got_norm)
                          if gn == wn and j not in matched_got), None)
            if exact is not None:
                matched_got.add(exact)
                continue
            free = next((j for j in range(len(got)) if j not in matched_got), None)
            if free is not None:
                matched_got.add(free)
                report.divergences.append(Divergence(
                    shot_id=unit_id, scene_id=str(head.get("scene_id") or ""),
                    kind="wrong", expected=line, found=got[free][1], speaker=spk,
                    detail=f"台词被改写：应为「{line[:30]}」，提示词写的是「{got[free][1][:30]}」"))
            else:
                report.divergences.append(Divergence(
                    shot_id=unit_id, scene_id=str(head.get("scene_id") or ""),
                    kind="missing", expected=line, speaker=spk,
                    detail=f"台词缺失：「{line[:30]}」没进提示词（成片会没这句人声）"))
    return report


def _official_line(speaker: str, text: str, english: bool) -> str:
    if english:
        return f'{speaker.strip() or "The character"} says: "{text.strip()}"'
    return f"{speaker.strip() or '角色'}说话：「{text.strip()}」"


def repair_dialogue(script: Script, shot_dicts: list[dict[str, Any]],
                    *, set_needs_review: bool = True) -> RepairReport:
    """把分叉的台词改回剧本原文（逐字），返回修复总账。

    改法（确定性，不靠模型）：
      · wrong（意译）→ 用正则把那条句式整段换成官方句式 + 原文台词；
      · missing（漏写）→ 按 _insert_dialogue 的口径插进动作链中段
        （这里简化：插到镜头正文第一句之后，组提示词插到对应机位块尾）；
      · 改过的镜头 prompt_status 打回 needs_review（台词变了必须重审，
        与「改参数打回」同权，D-43 精神）；已出片的镜头由调用方决定是否重出。

    只动台词，不动锚/运镜/风格段——最小侵入。返回的 report.divergences
    是**修复前**发现的问题清单（供人话汇报「修了哪几处」）。
    """
    report = find_divergences(script, shot_dicts)
    if not report.divergences:
        return report
    expected = expected_dialogue(script, shot_dicts)

    by_shot: dict[str, list[Divergence]] = {}
    for d in report.divergences:
        by_shot.setdefault(d.shot_id, []).append(d)

    for sid, divs in by_shot.items():
        head = next((s for s in shot_dicts if s.get("id") == sid), None)
        if head is None:
            continue
        field_name = "group_prompt" if head.get("group_prompt") else "video_prompt"
        prompt = str(head.get(field_name) or "")
        english = prompt_has_english(prompt)
        want = _shot_unit_lines(sid, shot_dicts, expected)

        # wrong：整段替换那条被改写的句式
        for d in divs:
            if d.kind != "wrong":
                continue
            rx = _DLG_RE_EN if english else _DLG_RE_ZH
            for m in list(rx.finditer(prompt)):
                if _norm(m.group("text")) == _norm(d.found):
                    fixed = _official_line(d.speaker, d.expected, english)
                    prompt = prompt[:m.start()] + fixed + prompt[m.end():]
                    break
        # missing：补插官方句式（插到正文第一句之后；找不到锚就追加末尾）
        for d in divs:
            if d.kind != "missing":
                continue
            line = _official_line(d.speaker, d.expected, english)
            if _norm(d.expected) in [ _norm(t) for _, t in extract_prompt_dialogue(prompt)]:
                continue  # 已被 wrong 分支修好
            prompt = _insert_line(prompt, line, english)

        head[field_name] = prompt
        if set_needs_review:
            head["prompt_status"] = "needs_review"
        report.repaired_shots.append(sid)

    report.notes.append(
        f"修复 {len(report.repaired_shots)} 个提交单元的台词"
        f"（{sum(1 for d in report.divergences if d.kind == 'missing')} 处漏写、"
        f"{sum(1 for d in report.divergences if d.kind == 'wrong')} 处改写）"
    )
    return report


def _insert_line(prompt: str, line: str, english: bool) -> str:
    """把台词句插进动作链中段（与 dp._insert_dialogue 同思路的简化版）。"""
    parts = [p for p in re.split(r"(?<=[。！？.!?])", prompt) if p.strip()]
    if len(parts) <= 1:
        sep = " " if english else "。"
        return (prompt.rstrip("。.") + sep + line).strip()
    sep = "" if english else "。"
    return sep.join([parts[0].rstrip("。.!？?"), line] + [p.rstrip("。.!？?") for p in parts[1:]]) + ("。" if not english else "")


def replace_text(shot_dicts: list[dict[str, Any]], old: str, new: str,
                 *, context: str = "", set_needs_review: bool = True) -> tuple[list[str], list[str]]:
    """带上下文的精确替换（即梦同款：唯一匹配才动，多处匹配要求带上下文定位）。

    返回 (改动的 shot_id 列表, 拒绝原因列表)。
    - old 在某镜头提示词里出现 0 次 → 跳过该镜头；
    - 出现 1 次 → 直接替换；
    - 出现多次且没给 context → 拒绝（不猜，即梦记录里「工具按唯一匹配逐处替换
      改不动」正是这个坑，Agent 后来改用带上下文精确定位）；
    - 给了 context → 只替换「紧邻 context」的那一处。
    改过的镜头打回 needs_review。
    """
    changed: list[str] = []
    rejected: list[str] = []
    if not old:
        return changed, ["old 文本不能为空"]
    for s in shot_dicts:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        field_name = "group_prompt" if s.get("group_prompt") else "video_prompt"
        prompt = str(s.get(field_name) or "")
        if old not in prompt:
            continue
        n = prompt.count(old)
        if n == 1:
            s[field_name] = prompt.replace(old, new)
        elif context:
            # 只动紧邻 context 的那一处：找 context 的位置，替换其后的第一个 old
            ci = prompt.find(context)
            if ci < 0:
                rejected.append(f"{s['id']}：出现 {n} 次但找不到上下文「{context[:20]}」，没动")
                continue
            oi = prompt.find(old, ci)
            if oi < 0:
                oi = prompt.find(old)  # 上下文在 old 之后时回退第一处
            s[field_name] = prompt[:oi] + new + prompt[oi + len(old):]
        else:
            rejected.append(
                f"{s['id']}：「{old[:20]}」出现 {n} 次，需要带上下文精确定位（别盲替换）")
            continue
        if set_needs_review:
            s["prompt_status"] = "needs_review"
        changed.append(str(s["id"]))
    return changed, rejected
