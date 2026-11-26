# =====================================================================
# script_doctor.py —— 剧本医生：进制作前的确定性体检（2026-11-20，C3）
#
# 对标即梦的「剧本医生综合诊断报告」（质量分/等级/可用性/送审建议/问题清单）。
# 差别在做法：即梦那份是 LLM 诊断，我们这份**全部确定性规则**——
# 复用链路上已有的三把尺子，零 LLM、零花费、可单测、每次跑结果一致：
#   · validate_script（引用/id 硬约束）
#   · story_plan 事件清单（扩写底线：事件有没有被写丢）
#   · rhythm 口播秒数（台词太长一场装不下）
# 外加两项只有规则能稳定抓的：钩子位置（第一集开场有没有事）、
# 过审风险词表（直陈暴力/裸露/药品——即梦记录里「全身裸露未过审」就是这类，
# 提前在文本层亮灯，别等出图出片被厂商拦了才发现）。
#
# 报告是给人看的：质量分 0-100、等级 A/B/C、可用性一句话、
# 送审建议 pass / pass_with_check / needs_review、问题按严重度分组。
# =====================================================================

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..domain import Script
from ..pipeline.patch import validate_script
from ..video.rhythm import speech_need_seconds

# 过审风险词表（直陈式）：出图/出片厂商审核的高频拦截点。
# 只抓「直陈画面」的写法——暗示式（「倒下了」「起效了」）不在表内，
# 导演技能的内容安全边界就是这么教的，这里抓的是没做到的漏网。
_SAFETY_RE = re.compile(
    r"枪支|手枪上膛|持刀|捅了|割腕|自杀|上吊|毒品|注射器|"
    r"裸露|全裸|赤身|衣衫不整地?躺|血迹斑斑|脑浆|断肢"
)

# 一场戏台词口播的软上限（秒）：超了说明这段对白该拆场/拆镜了。
_SCENE_DIALOGUE_SOFT_SEC = 30.0


@dataclass
class DoctorIssue:
    """一条体检发现。"""

    severity: str          # error / warn / info
    area: str              # 结构 / 事件 / 台词 / 节奏 / 过审
    text: str


@dataclass
class DoctorReport:
    issues: list[DoctorIssue] = field(default_factory=list)
    score: int = 100
    grade: str = "A"
    usability: str = ""
    submission: str = "pass"          # pass / pass_with_check / needs_review
    stats: dict[str, Any] = field(default_factory=dict)

    def to_markdown(self, title: str = "") -> str:
        lines = [f"# 剧本医生体检报告{('：' + title) if title else ''}", ""]
        lines.append(f"- 质量分：**{self.score} / 100**（等级 {self.grade}）")
        lines.append(f"- 可用性：{self.usability}")
        sub = {"pass": "pass（可进制作）",
               "pass_with_check": "pass with check（建议先处理标注项再进制作）",
               "needs_review": "needs review（先修问题，别急着花钱出片）"}[self.submission]
        lines.append(f"- 送审/进制作建议：**{sub}**")
        s = self.stats
        lines.append(
            f"- 评估范围：{s.get('scenes', 0)} 场 / {s.get('beats', 0)} 节拍 / "
            f"{s.get('dialogue', 0)} 句台词"
            + (f" / 对照事件清单 {s.get('events', 0)} 件" if s.get("events") else "")
            + (f" / 段落决策 {s.get('segments', 0)} 段 · 目标 {s.get('target_sec', 0):.0f} 秒"
               if s.get("segments") else ""))
        for sev, head in (("error", "## 必须修"), ("warn", "## 建议修"), ("info", "## 参考")):
            items = [i for i in self.issues if i.severity == sev]
            if not items:
                continue
            lines.append("")
            lines.append(head)
            for i in items:
                lines.append(f"- [{i.area}] {i.text}")
        return "\n".join(lines)


def _check_structure(script: Script, out: list[DoctorIssue]) -> None:
    for v in validate_script(script):
        sev = "error" if v.severity == "error" else "warn"
        out.append(DoctorIssue(sev, "结构", f"{v.path}：{v.message}"))
    for sc in script.scenes:
        if not sc.beats:
            out.append(DoctorIssue("error", "结构", f"{sc.id}《{sc.title}》没有任何节拍"))
        # 兜底话是 _SceneIn 的默认值「推进情节。」（带句号）——前缀匹配，
        # 模型照抄默认值也要抓到。
        if not (sc.purpose or "").strip() or (sc.purpose or "").startswith("推进情节"):
            out.append(DoctorIssue("warn", "结构", f"{sc.id}《{sc.title}》目的没填或是兜底话"))
        if not sc.time:
            out.append(DoctorIssue("warn", "结构", f"{sc.id}《{sc.title}》没标昼夜（提示词光线会缺依据）"))


def _check_events(script: Script, plan: Any, out: list[DoctorIssue]) -> int:
    """扩写底线：提案事件有没有被写丢（关键词命中节拍文本即算覆盖）。"""
    if plan is None or not getattr(plan, "understanding", None):
        return 0
    corpus = "".join(
        (b.text or "") + (b.line or "") for sc in script.scenes for b in sc.beats
    )
    missing = []
    for ev in plan.understanding.events:
        # 取事件文本里最长的 2-4 字片段做探针（人名/道具/动作词）
        tokens = [t for t in re.split(r"[\s，。、；：「」“”（）\-/]+", ev.text) if len(t) >= 2]
        probe = max(tokens, key=len) if tokens else ""
        if probe and probe not in corpus:
            missing.append(ev)
    for ev in missing[:8]:
        out.append(DoctorIssue("error", "事件",
                               f"提案事件「{ev.text[:30]}」在剧本里找不到对应节拍（被写丢了？）"))
    if len(missing) > 8:
        out.append(DoctorIssue("error", "事件", f"另有 {len(missing) - 8} 件提案事件未覆盖"))
    return len(plan.understanding.events)


def _check_dialogue(script: Script, out: list[DoctorIssue]) -> int:
    n = 0
    for sc in script.scenes:
        sec = sum(speech_need_seconds(b.line or "") for b in sc.beats if b.type == "dialogue")
        for b in sc.beats:
            if b.type == "dialogue" and (b.line or "").strip():
                n += 1
                if not b.speaker:
                    out.append(DoctorIssue("error", "台词",
                                           f"{sc.id} 的 beat {b.id} 没有说话人（配音/字幕会哑）"))
        if sec > _SCENE_DIALOGUE_SOFT_SEC:
            out.append(DoctorIssue("warn", "台词",
                                   f"{sc.id}《{sc.title}》台词口播约 {sec:.0f} 秒，"
                                   "超一场软上限——建议拆场或精简对白"))
    return n


def _check_hook(script: Script, out: list[DoctorIssue]) -> None:
    """开场钩子：第一场的头两个节拍里要有对白或位移级动作（3 秒抓住注意力）。"""
    if not script.scenes:
        return
    from ..crew.director import _MOVEMENT_RE

    head = script.scenes[0].beats[:2]
    has = any(b.type == "dialogue" and (b.line or "").strip() for b in head) or \
        any(_MOVEMENT_RE.search(b.text or "") for b in head)
    if not has:
        out.append(DoctorIssue("warn", "节奏",
                               "开场两个节拍既无对白也无位移级动作——前 3 秒没钩子，观众会划走"))


def _check_safety(script: Script, out: list[DoctorIssue]) -> int:
    hits = 0
    for sc in script.scenes:
        for b in sc.beats:
            text = (b.text or "") + (b.line or "")
            for m in _SAFETY_RE.finditer(text):
                hits += 1
                out.append(DoctorIssue(
                    "warn", "过审",
                    f"{sc.id} beat {b.id} 直陈「{m.group(0)}」——厂商审核高频拦截点，"
                    "建议改暗示式（只写存在与结果，不写过程）"))
    return hits


def _check_adaptation(script: Script, plan: Any, out: list[DoctorIssue]) -> int:
    """段落级改编决策的落地检查（D-90）。

    must_keep 覆盖 / cut 清理 / 时长带 / 人物焦点复用
    pipeline.adaptation.coverage_issues 的同一套判据；节拍预算在这里补一刀。
    """
    if plan is None:
        return 0
    from ..pipeline.adaptation import coverage_issues

    for text in coverage_issues(script, plan):
        if text.startswith("错误："):
            sev, msg = "error", text.removeprefix("错误：")
        elif text.startswith("建议："):
            sev, msg = "warn", text.removeprefix("建议：")
        else:
            sev, msg = "info", text
        out.append(DoctorIssue(sev, "改编", msg))

    for sc in script.scenes:
        seg_ids = (getattr(plan, "scene_segments", {}) or {}).get(sc.id, [])
        if not seg_ids:
            continue
        expected = 0
        for sid in seg_ids:
            seg = plan.segment(sid) if hasattr(plan, "segment") else None
            if seg is not None:
                expected += int(getattr(seg, "target_beats", 0) or 0)
        if expected and abs(len(sc.beats) - expected) > max(2, expected * 0.5):
            out.append(DoctorIssue(
                "warn", "改编",
                f"{sc.id} 写了 {len(sc.beats)} 拍，段落决策给的是 {expected} 拍："
                "多出来的多是反应/过场，检查是否注水；少了的可能漏拍"))
    return len(getattr(plan, "segments", []) or [])


def _check_text(script: Script, out: list[DoctorIssue]) -> None:
    """剧本文本层体检（2026-11-26）：场头/人物/英文/文言/不可拍/重复/卡点。"""
    from ..pipeline.script_text import audit_script_text

    for text in audit_script_text(script):
        out.append(DoctorIssue("warn", "文本", text))


def doctor_script(script: Script, plan: Any = None, adaptation_plan: Any = None) -> DoctorReport:
    """跑一遍确定性体检，返回带分数/等级/建议的报告。"""
    issues: list[DoctorIssue] = []
    _check_structure(script, issues)
    n_events = _check_events(script, plan, issues)
    n_dlg = _check_dialogue(script, issues)
    _check_hook(script, issues)
    n_safety = _check_safety(script, issues)
    n_seg = _check_adaptation(script, adaptation_plan, issues)
    # 旧剧本没有语体/场头字段，不拿新格式判据去打它们。
    if getattr(script, "dialogue_style", "") or any(
            sc.interior_exterior for sc in script.scenes):
        _check_text(script, issues)

    n_err = sum(1 for i in issues if i.severity == "error")
    n_warn = sum(1 for i in issues if i.severity == "warn")
    score = max(0, 100 - n_err * 12 - n_warn * 4)
    grade = "A" if score >= 80 else ("B" if score >= 60 else "C")
    if n_err:
        usability = "不合格：有必须修的问题，进制作会一路带病"
        submission = "needs_review"
    elif n_safety:
        usability = "结构合格，有过审风险点"
        submission = "pass_with_check"
    elif n_warn:
        usability = "合格可用，有少量建议项"
        submission = "pass"
    else:
        usability = "合格可用"
        submission = "pass"

    return DoctorReport(
        issues=issues, score=score, grade=grade, usability=usability,
        submission=submission,
        stats={"scenes": len(script.scenes),
               "beats": sum(len(sc.beats) for sc in script.scenes),
               "dialogue": n_dlg, "events": n_events,
               "segments": n_seg,
               "target_sec": float(getattr(adaptation_plan, "target_sec", 0) or 0)},
    )
