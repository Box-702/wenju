# =====================================================================
# skill_eval.py —— 技能加载 eval（让「拆成四个技能」变成可测量的结论）
#
# 核心是**消融对比**（ablation）：同一批用例跑两遍——
#
#   有技能：load_skill 可用，Agent 能拉到 SKILL.md 正文
#   无技能：把 load_skill 从工具集里摘掉，Agent 只能靠系统提示的触发器表
#
# 两组的行为差值，就是**技能正文（细则）的真实价值**。
# 因为系统提示本来就有「意图 → 工具」的触发器表，所以：
#   - 只看「调对工具」→ 证明不了技能有用（触发器表就够）；
#   - 看「细则里的硬约束有没有落地」→ 才能证明。
#
# 用法：
#   python -m evals.skill_eval                # 只跑有技能
#   python -m evals.skill_eval --ablate       # 两组都跑并对比
#   python -m evals.skill_eval --case crew_order
# =====================================================================

from __future__ import annotations

import argparse
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.domain import StyleGuide

from .skill_cases import (
    ALL_SKILL_CASES,
    SkillCase,
    called_tools,
    index_of,
    loaded_skills,
)

log = logging.getLogger(__name__)

#: 逐次明细是否打印（默认只打聚合表；排查时用 --verbose）
_VERBOSE = False

#: 默认原文（用例没给 raw_text 时用）
DEFAULT_RAW_TEXT = "第一章 雨夜" + chr(10) + "林然在巷口遇上一个红雨衣男人。"


# ---------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------


@dataclass
class CaseOutcome:
    """一条用例在某一条件下的结果。"""

    case_id: str
    condition: str  # "skills" / "no_skills"
    ok: bool = False
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    reply: str = ""
    error: str = ""

    @property
    def passed_checks(self) -> int:
        return sum(1 for _, ok, _ in self.checks if ok)

    @property
    def total_checks(self) -> int:
        return len(self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case_id,
            "condition": self.condition,
            "ok": self.ok,
            "passed_checks": self.passed_checks,
            "total_checks": self.total_checks,
            "loaded_skills": self.loaded,
            "tools": self.tools,
            "reply_head": self.reply[:300],
            "error": self.error,
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in self.checks],
        }


@dataclass
class EvalReport:
    outcomes: list[CaseOutcome] = field(default_factory=list)

    def by_condition(self, cond: str) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.condition == cond]

    @property
    def rule_rate(self) -> dict[str, float]:
        """每个条件下「细则约束」的落地率（关键指标）。"""
        out = {}
        for cond in ("skills", "no_skills"):
            outs = self.by_condition(cond)
            total = sum(o.total_checks for o in outs)
            passed = sum(o.passed_checks for o in outs)
            out[cond] = (passed / total) if total else 0.0
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_rate": self.rule_rate,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }

    def render(self) -> str:
        lines = ["=" * 72, "技能加载 eval 报告", "=" * 72]
        for cond in ("skills", "no_skills"):
            outs = self.by_condition(cond)
            if not outs:
                continue
            total = sum(o.total_checks for o in outs)
            passed = sum(o.passed_checks for o in outs)
            label = "有技能（可 load_skill）" if cond == "skills" else "无技能（屏蔽 load_skill）"
            lines.append(f"\n【{label}】约束落地率 {passed}/{total} = {self.rule_rate[cond]:.0%}")

        # 按用例聚合：逐次输出太吵，看的是「这条用例的约束在多次运行里落地的比例」
        table = self.by_case_table()
        if table:
            lines.append("")
            lines.append("按用例聚合（约束落地比例，多次运行合并）：")
            lines.append(f"  {'用例':<28}{'有技能':>12}{'无技能':>12}{'差':>8}")
            for case_id, (a, b) in table.items():
                da = (a[0] / a[1] * 100) if a[1] else 0.0
                db = (b[0] / b[1] * 100) if b[1] else 0.0
                sa = f"{a[0]}/{a[1]}" if a[1] else "—"
                sb = f"{b[0]}/{b[1]}" if b[1] else "—"
                lines.append(f"  {case_id:<28}{sa:>12}{sb:>12}{da - db:>+7.0f}%")

        # 逐次明细只在需要排查时看
        if _VERBOSE:
            for cond in ("skills", "no_skills"):
                outs = self.by_condition(cond)
                if not outs:
                    continue
                label = "有技能" if cond == "skills" else "无技能"
                lines.append(f"\n【{label}】逐次明细")
                for o in outs:
                    mark = "OK  " if o.ok else "FAIL"
                    lines.append(f"  [{mark}] {o.case_id:<26} 载入 {o.loaded or '（无）'}")
                    for name, ok, detail in o.checks:
                        lines.append(f"         {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
                    if o.error:
                        lines.append(f"         错误：{o.error}")

        if self.by_condition("skills") and self.by_condition("no_skills"):
            a, b = self.rule_rate["skills"], self.rule_rate["no_skills"]
            lines.append("")
            lines.append(f"Δ 消融差值（有技能 − 无技能）= {a - b:+.0%}")
            if a - b <= 0.02:
                lines.append("  ⚠ 没有可观测差异 —— 技能正文目前没起作用，考虑合并或补规则")
            else:
                lines.append("  ✓ 技能正文带来了可观测的行为差异")
        elif self.by_condition("skills"):
            lines.append("")
            lines.append("（未跑消融对照，加 --ablate 可看到技能正文的实际贡献）")
        return "\n".join(lines)

    def by_case_table(self) -> dict[str, tuple[tuple[int, int], tuple[int, int]]]:
        """按用例聚合两种条件下的约束落地比例。"""
        agg: dict[str, dict[str, list[int]]] = {}
        for o in self.outcomes:
            cell = agg.setdefault(o.case_id, {}).setdefault(o.condition, [0, 0])
            cell[0] += o.passed_checks
            cell[1] += o.total_checks
        return {
            case: (
                tuple(conds.get("skills", [0, 0])),   # type: ignore[misc]
                tuple(conds.get("no_skills", [0, 0])),  # type: ignore[misc]
            )
            for case, conds in agg.items()
        }


# ---------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------


def evaluate(case: SkillCase, events: list[dict[str, Any]], reply: str) -> list[tuple[str, bool, str]]:
    """把一次对话的结果对着一组期望逐条判定。"""
    tools = called_tools(events)
    loaded = loaded_skills(events)
    checks: list[tuple[str, bool, str]] = []

    # 1) 该加载的加载了
    if case.expect_skill:
        checks.append((
            f"加载了 {case.expect_skill}",
            case.expect_skill in loaded,
            f"实际载入：{'、'.join(loaded) or '（无）'}",
        ))
    else:
        checks.append((
            "没有过度触发（一个技能都没加载）",
            not loaded,
            f"实际载入：{'、'.join(loaded) or '（无）'}",
        ))

    # 2) 没有串味
    bad = [s for s in case.forbid_skills if s in loaded]
    checks.append((
        "没有加载不相关技能",
        not bad,
        f"串味：{'、'.join(bad)}" if bad else "",
    ))

    # 3) 该用的工具用了
    for t in case.expect_tools:
        checks.append((f"调用了 {t}", t in tools, f"实际：{'、'.join(tools) or '（无）'}"))

    # 4) 不该用的工具没用
    for t in case.forbid_tools:
        checks.append((f"没有调用 {t}", t not in tools, f"实际：{'、'.join(tools) or '（无）'}"))

    # 5) 相对顺序（依赖关系：后面的必须在前面的之后）
    if case.expect_order:
        positions = [(t, index_of(tools, t)) for t in case.expect_order]
        missing = [t for t, i in positions if i < 0]
        if missing:
            checks.append((
                "按依赖顺序调用",
                False,
                f"缺少：{'、'.join(missing)}（实际：{'、'.join(tools) or '（无）'}）",
            ))
        else:
            ok_order = all(
                positions[i][1] < positions[i + 1][1] for i in range(len(positions) - 1)
            )
            checks.append((
                f"按依赖顺序调用（{' → '.join(case.expect_order)}）",
                ok_order,
                f"实际：{'、'.join(tools)}",
            ))

    # 6) 回复里提到了关键点
    if case.expect_reply_contains:
        hit = [k for k in case.expect_reply_contains if k in reply]
        checks.append((
            "回复提到了关键约束",
            bool(hit),
            f"命中：{'、'.join(hit) or '（无）'}",
        ))

    return checks


# ---------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------


def _make_env(tmpdir: Path):
    """建一套隔离环境：临时 SQLite + 真实模型 + 临时项目。"""
    from app.config import Settings
    from app.llm import build_llm
    from app.store import Store

    settings = Settings(
        DATABASE_URL=f"sqlite:///{(tmpdir / 'eval.db').as_posix()}",
        CHECKPOINTER="memory",
    )
    store = Store(settings.database_url)
    llm = build_llm(settings, store)
    return settings, store, llm


def _prepare_project(store: Any, llm: Any, settings: Any, project: Any, level: str) -> str:
    """预置项目内容，让领域动作**可执行**。

    level="script"：生成剧本版本（否则 Agent 只能回「还没有剧本」，
                    测出来的是「它知道自己没剧本」而不是「技能引导了它」）
    level="shots" ：再往下跑到分镜 + 视频提示词，全部标成**待批准**
                    （只有真的有未批准的镜头，「出片前要先批准」才被考验）
    """
    from app.crew.art_director import run_art_director
    from app.crew.director import run_director
    from app.crew.dp import run_dp
    from app.pipeline.generation import generate_script

    script, _artifacts = generate_script(
        llm, settings,
        title=project.title, raw_text=project.raw_text,
        adaptation_type=project.adaptation_type, language=project.language,
    )
    store.create_version(
        project, script, source_type="generation", label="eval 初稿", set_current=True,
    )
    if level == "script":
        return ""

    d = run_director(llm, script)
    if not d.success or not isinstance(d.data, list):
        return f"导演拆解失败：{getattr(d, 'summary', '')}"
    art = run_art_director(llm, script, language="zh")
    style_guide = art.data if (art.success and isinstance(art.data, StyleGuide)) else None
    dp = run_dp(
        llm, script, breakdowns=d.data, style_guide=style_guide,
        target_provider="minimax", provider_label="MiniMax", max_duration=6.0,
    )
    if not dp.success or not isinstance(dp.data, list):
        return f"摄影指导失败：{getattr(dp, 'summary', '')}"
    shots = []
    for s in dp.data:
        data = s.model_dump()
        data["prompt_status"] = "needs_review"
        data["prompt_review_note"] = ""
        shots.append(data)
    store.create_video_version(
        project_id=project.id, shots=shots,
        source_type="agent", label="eval 待批准镜头",
    )
    return ""


def _prepare_case(case: SkillCase, store: Any, llm: Any, settings: Any) -> str | None:
    """建项目并按需预置内容，返回 project_id（失败时返回 None）。"""
    if not case.need_project:
        return None
    project = store.create_project(
        title=f"eval-{case.id}",
        adaptation_type="short_drama",
        language="zh-CN",
        raw_text=case.raw_text or DEFAULT_RAW_TEXT,
    )
    if case.prepare:
        err = _prepare_project(store, llm, settings, project, case.prepare)
        if err:
            log.warning("预置内容失败（%s）：%s", case.id, err)
            return None
    return project.id


def _run_chat(
    case: SkillCase, store: Any, llm: Any, settings: Any, project_id: str | None
) -> tuple[list[dict], str, str]:
    """跑一次对话，返回 (events, reply, error)。"""
    from app.chat import conductor

    conv = store.ensure_default_conversation(project_id)
    try:
        result = conductor.chat_once(
            store, llm, settings,
            conversation_id=conv.id, project_id=project_id,
            message=case.request,
        )
        return result.get("events") or [], result.get("reply") or "", ""
    except Exception as e:  # noqa: BLE001
        log.exception("用例 %s 执行失败", case.id)
        return [], "", f"{type(e).__name__}: {e}"


def _disable_skills() -> None:
    """把 load_skill 从工具集里摘掉——模拟「没有技能系统」。"""
    from app.chat import conductor

    original = conductor.build_chat_tools

    def patched(*args, **kwargs):
        tools = original(*args, **kwargs)
        return [t for t in tools if getattr(t, "name", "") != "load_skill"]

    conductor.build_chat_tools = patched  # type: ignore[assignment]
    # 记忆工具模块里的原函数也留个引用，方便还原
    conductor._ORIGINAL_BUILD_TOOLS = original  # type: ignore[attr-defined]


def _restore_skills() -> None:
    from app.chat import conductor

    original = getattr(conductor, "_ORIGINAL_BUILD_TOOLS", None)
    if original is not None:
        conductor.build_chat_tools = original  # type: ignore[assignment]
        del conductor._ORIGINAL_BUILD_TOOLS  # type: ignore[attr-defined]


def run(cases: list[SkillCase], *, ablate: bool = False, repeat: int = 3) -> EvalReport:
    report = EvalReport()
    conditions = ["skills", "no_skills"] if ablate else ["skills"]

    with tempfile.TemporaryDirectory(prefix="skill_eval_") as tmp:
        tmpdir = Path(tmp)
        for cond in conditions:
            if cond == "no_skills":
                _disable_skills()
            store = None
            try:
                settings, store, llm = _make_env(tmpdir / cond)
                if not llm.available:
                    log.warning("没有可用模型，跳过条件 %s", cond)
                    continue
                for case in cases:
                    project_id = _prepare_case(case, store, llm, settings)
                    for rep in range(repeat):
                        print(f"  [{cond}] {case.id}  第{rep + 1}/{repeat} 次 …", flush=True)
                        events, reply, err = _run_chat(case, store, llm, settings, project_id)
                        checks = evaluate(case, events, reply)
                        report.outcomes.append(CaseOutcome(
                            case_id=case.id,
                            condition=cond,
                            ok=bool(checks) and all(ok for _, ok, _ in checks),
                            checks=checks,
                            loaded=loaded_skills(events),
                            tools=called_tools(events),
                            reply=reply,
                            error=err,
                        ))
            finally:
                # 必须先断开数据库连接，否则 Windows 上临时目录清不掉（文件被占用）
                if store is not None:
                    try:
                        store.engine.dispose()
                    except Exception:  # noqa: BLE001
                        pass
                if cond == "no_skills":
                    _restore_skills()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="技能加载 eval")
    parser.add_argument("--ablate", action="store_true", help="同时跑「屏蔽 load_skill」的对照组")
    parser.add_argument("--case", default="", help="只跑指定用例 id（逗号分隔）")
    parser.add_argument("--out", default="", help="JSON 报告输出路径")
    parser.add_argument("--verbose", action="store_true", help="打印逐次明细")
    parser.add_argument("--repeat", type=int, default=3,
                        help="每条用例重复几次（单次方差大，结论不可靠；默认 3）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    from dotenv import load_dotenv

    load_dotenv()

    cases = ALL_SKILL_CASES
    if args.case:
        wanted = {c.strip() for c in args.case.split(",") if c.strip()}
        cases = [c for c in cases if c.id in wanted]

    global _VERBOSE
    _VERBOSE = args.verbose
    cond_note = "（含消融对照）" if args.ablate else ""
    print(f"技能加载 eval：{len(cases)} 条用例 × {max(1, args.repeat)} 次 {cond_note}")
    report = run(cases, ablate=args.ablate, repeat=max(1, args.repeat))
    print()
    print(report.render())

    if args.out:
        Path(args.out).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
