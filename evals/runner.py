# =====================================================================
# runner.py —— 提示词质量 eval 引擎
#
# 跑一条链路：导演拆解 → 美术指导 → 摄影指导 → 两层判定 → 报告。
#
#   确定性层（assertions.py）：规则判定，永远稳定，CI 必跑
#   judge 层（本文件）：LLM 按 rubric.md 打分，看趋势
#
# judge 结果**不参与 CI 红绿判定**——LLM 打分有抖动，
# 放进 CI 只会制造随机失败。CI 只卡确定性的 critical 数。
# =====================================================================

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.crew.art_director import run_art_director
from app.crew.director import run_director
from app.crew.dp import run_dp
from app.domain import SceneBreakdown, Script, Shot, StyleGuide
from app.llm import LLM

from .assertions import AssertionResult, assert_director, assert_dp, assert_pipeline, summarize
from .cases import EvalCase

log = logging.getLogger(__name__)

# 目标视频 provider（决定提示词方言）。默认 minimax——产品统一用它。
DEFAULT_PROVIDER = "minimax"
DEFAULT_PROVIDER_LABEL = "MiniMax"
DEFAULT_MAX_DURATION = 6.0

# 每条 case 最多送几个镜头给 judge（控制成本与时长）。
JUDGE_MAX_SHOTS = 3


# ---------------------------------------------------------------------
# Judge 提示词（与 docs/judge_prompt.md 保持同步）
# ---------------------------------------------------------------------

JUDGE_SYSTEM = """\
你是一位严格但有据的短剧提示词评审员。你要评估的是「交给视频生成模型的镜头提示词」，
以及「导演拆出的分镜方案」。

## 你的任务
按下面五个维度给 0-100 分，每个维度必须给出**从原文摘抄的证据片段**。

## 绝对规则（违反其中任何一条，本次评审作废）
1. 只依据「待评审内容」里写出来的东西打分。**不得假设没写出来的内容存在**——
   没写环境细节不等于环境细节差，而是这条提示词没写；没写人物造型不等于造型崩了。
2. 每一条扣分都必须附一段**原文引用**（不超过 40 字）。引用不出来，就不许扣这一分。
3. 不给改进建议、不改写、不额外评论。只输出分数与证据。
4. 不要因为「整体读起来还不错」就加分。高分必须靠具体维度一条条攒出来。
5. 分数要拉开差距：真正优秀的给 90+，合格给 70–85，勉强能用的给 50–69，
   有明显硬伤的给 30 以下。不要把所有输出都打在 80 分附近。

## 五个维度

### 1. 戏剧性（镜头有没有做功）
看这个镜头是否改变了情绪、推进了动作或施加了压力，且变化是**画面可见的**。
- 90+：变化明确且可见，不解释也能读懂
- 70-89：做了功但需脑补
- 40-69：只记录状态，不推进
- <40：壁纸镜头，换任何场景都成立

### 2. 可渲染性（模型能不能照着画）
看每件事是否有具体物理形态：位置关系、身体姿态、接触点、光从哪来、东西怎么动。
- 90+：全部可渲染
- 70-89：主要动作可渲染，部分抽象
- 40-69：大量抽象描述
- <40：几乎全是抽象判断

抽象概念（如"意识到""犹豫""回忆起"）不扣分——但如果提示词**只**写了这类概念
而没有任何物理事实，可渲染性给低分。

### 3. 一致性（和上下游对不对得上）
看人物造型、环境描述、光线方向是否与给定的前序信息一致。
- 只有在提供了「前序信息」时才评这一维；没提供就标 not_applicable。

### 4. 具体度（细节密度）
看是否同时具备：环境压力、身体微动作、声音/视觉母题。
- 90+：三类都有；70-89：两类；40-69：一类；<40：零细节

### 5. 克制度（有没有 AI 味）
看有没有：空洞形容词（电影感/史诗感/震撼/cinematic/epic…）、一镜多运镜、
否定式描述（不要/没有/not…）、把台词写进提示词、动作过载、越轴运镜。

## 输出格式（严格 JSON，不要任何额外文字）

{
  "scores": {
    "dramatic": {"score": 0-100, "evidence": "原文引用（无则空字符串）"},
    "renderable": {"score": 0-100, "evidence": "原文引用"},
    "consistency": {"score": 0-100, "evidence": "原文引用", "not_applicable": false},
    "specificity": {"score": 0-100, "evidence": "原文引用"},
    "restraint": {"score": 0-100, "evidence": "原文引用"}
  },
  "total": 0-100,
  "critical_issues": ["致命问题（若有），每条不超过 40 字"]
}

total = dramatic×0.25 + renderable×0.25 + consistency×0.20 + specificity×0.15 + restraint×0.15
（consistency 标了 not_applicable 时，用其余四维重新归一化）

critical_issues 只填真正会让出片失败的硬伤；没有问题就给空数组。
"""

_DIM_WEIGHTS = {
    "dramatic": 0.25,
    "renderable": 0.25,
    "consistency": 0.20,
    "specificity": 0.15,
    "restraint": 0.15,
}


# ---------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------


@dataclass
class JudgeScore:
    """单个镜头的 judge 打分。"""

    shot_id: str
    total: float = 0.0
    dims: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    critical_issues: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class CaseResult:
    """一条 case 的完整 eval 结果。"""

    case: EvalCase
    ok: bool = True
    error: str = ""
    #: 被基础设施问题挡住（如模型余额耗尽）——**不是质量失败**，单独计数
    blocked: bool = False
    blocked_reason: str = ""

    breakdowns: list[SceneBreakdown] = field(default_factory=list)
    style_guide: StyleGuide | None = None
    shots: list[Shot] = field(default_factory=list)

    assertions: list[AssertionResult] = field(default_factory=list)
    judge_scores: list[JudgeScore] = field(default_factory=list)
    metadata_note: str = ""

    @property
    def critical_count(self) -> int:
        return sum(len(r.critical) for r in self.assertions)

    @property
    def warn_count(self) -> int:
        return sum(len(r.warns) for r in self.assertions)

    @property
    def judge_total(self) -> float | None:
        if not self.judge_scores:
            return None
        return sum(s.total for s in self.judge_scores) / len(self.judge_scores)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case.id,
            "name": self.case.name,
            "ok": self.ok,
            "blocked": self.blocked,
            "error": self.error,
            "note": self.metadata_note,
            "shots": len(self.shots),
            "critical": self.critical_count,
            "warn": self.warn_count,
            "judge_total": round(self.judge_total, 1) if self.judge_total is not None else None,
            "judge_dims": _avg_dims(self.judge_scores),
            "failures": [
                {
                    "rule": f.rule,
                    "severity": f.severity,
                    "where": f.where,
                    "message": f.message,
                }
                for r in self.assertions
                for f in r.failures
            ],
            "judge": [
                {
                    "shot_id": s.shot_id,
                    "total": round(s.total, 1),
                    "dims": {k: round(v, 1) for k, v in s.dims.items()},
                    "evidence": s.evidence,
                    "critical_issues": s.critical_issues,
                    "error": s.error,
                }
                for s in self.judge_scores
            ],
        }


@dataclass
class EvalReport:
    """一次 eval 全量跑完的汇总。"""

    results: list[CaseResult] = field(default_factory=list)
    provider: str = DEFAULT_PROVIDER
    model: str = ""

    @property
    def critical_total(self) -> int:
        return sum(r.critical_count for r in self.results)

    @property
    def warn_total(self) -> int:
        return sum(r.warn_count for r in self.results)

    @property
    def judge_average(self) -> float | None:
        scores = [r.judge_total for r in self.results if r.judge_total is not None]
        return sum(scores) / len(scores) if scores else None

    @property
    def blocked_total(self) -> int:
        return sum(1 for r in self.results if r.blocked)

    @property
    def failed_total(self) -> int:
        return sum(1 for r in self.results if not r.ok and not r.blocked)

    @property
    def passed(self) -> bool:
        """硬红线：确定性层 critical 必须为 0，且没有 case 跑挂。

        被基础设施挡住的 case（余额耗尽等）不计入失败——那是环境问题不是质量回归，
        但必须在报告里显式列出，否则「跑不动」会被误读成「跑过了且通过」。
        """
        return self.critical_total == 0 and self.failed_total == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "cases": len(self.results),
            "critical_total": self.critical_total,
            "warn_total": self.warn_total,
            "blocked_total": self.blocked_total,
            "failed_total": self.failed_total,
            "judge_average": round(self.judge_average, 1) if self.judge_average is not None else None,
            "passed": self.passed,
            "by_rule": _count_by_rule(self.results),
            "avg_dims": _avg_dims([s for r in self.results for s in r.judge_scores]),
            "results": [r.to_dict() for r in self.results],
        }

    def render(self) -> str:
        """渲染成可读报告。"""
        lines: list[str] = []
        lines.append("=" * 68)
        lines.append("提示词质量 eval 报告")
        # provider 指的是「目标视频方言」，不是对话模型——两者分开写，
        # 否则很容易读成「用 minimax 的模型跑 deepseek-flash」。
        lines.append(
            f"视频方言={self.provider}  对话模型={self.model or '(未记录)'}"
        )
        lines.append("=" * 68)

        for r in self.results:
            if r.blocked:
                mark = "SKIP"
            elif r.ok and r.critical_count == 0:
                mark = "OK  "
            elif r.ok:
                mark = "FAIL"
            else:
                mark = "ERR "
            jt = f"judge={r.judge_total:.1f}" if r.judge_total is not None else "judge=—"
            lines.append(
                f"[{mark}] {r.case.id:<20} {r.case.name:<14} "
                f"shots={len(r.shots):<3} critical={r.critical_count:<3} warn={r.warn_count:<3} {jt}"
            )
            if r.error:
                lines.append(f"      错误：{r.error}")
            if r.metadata_note:
                lines.append(f"      说明：{r.metadata_note}")
            for f in [x for res in r.assertions for x in res.failures]:
                lines.append(f"      {f}")

        lines.append("-" * 68)
        lines.append(f"确定性层：critical={self.critical_total}  warn={self.warn_total}")
        if self.blocked_total:
            lines.append(f"被基础设施挡住：{self.blocked_total} 条（不计入失败，但需要处理）")
        if self.judge_average is not None:
            lines.append(f"judge 平均分：{self.judge_average:.1f}")
        else:
            lines.append("judge 平均分：（未运行）")
        by_rule = _count_by_rule(self.results)
        if by_rule:
            lines.append("按规则统计：")
            for rule, n in by_rule.items():
                lines.append(f"  {rule}: {n}")
        lines.append(f"结论：{self._verdict()}")
        return "\n".join(lines)

    def _verdict(self) -> str:
        """三态结论：通过 / 未通过 / 无法评估。

        `critical_total == 0` 在「一条都没跑起来」时也成立——如果只输出「通过」，
        余额耗尽的那天 CI 会亮绿灯，而实际上什么都没评估。被挡住的 case 单独成一态。
        """
        total = len(self.results)
        if total and self.blocked_total >= total:
            return "无法评估（全部 case 被基础设施挡住，没有真正跑到）"
        if not self.passed:
            return "未通过（存在 critical 或 case 失败）"
        if self.blocked_total:
            return f"通过，但有 {self.blocked_total} 条未评估（基础设施问题，需补跑）"
        return "通过"

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)


def _avg_dims(scores: list[JudgeScore]) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for s in scores:
        for k, v in s.dims.items():
            acc.setdefault(k, []).append(v)
    return {k: round(sum(v) / len(v), 1) for k, v in acc.items() if v}


# 基础设施问题的特征串：余额不足、限流、鉴权失败、网络不可达。
# 这些**不是提示词质量回归**，必须与质量失败分开计数。
_BLOCK_MARKERS = (
    "insufficient_balance",
    "insufficient balance",
    "402",
    "rate_limit",
    "429",
    "invalid_api_key",
    "401",
    "connection",
    "timeout",
)


def _classify_block(exc: BaseException) -> str:
    """判断异常是否属于基础设施问题（而非质量回归）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    for marker in _BLOCK_MARKERS:
        if marker.lower() in text:
            return f"{type(exc).__name__}: {exc}"[:200]
    return ""


def _mark_blocked(res: CaseResult) -> None:
    """把 case 标记为「被基础设施挡住」——不算质量失败。"""
    reason = _classify_block(RuntimeError(res.error))
    if reason:
        res.blocked = True
        res.blocked_reason = reason
        res.error = f"被基础设施挡住：{reason}"
    else:
        res.ok = False


def _count_by_rule(results: list[CaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        for res in r.assertions:
            for f in res.failures:
                counts[f.rule] = counts.get(f.rule, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------
# judge 输出解析（容错）
# ---------------------------------------------------------------------


def parse_judge_output(text: str) -> tuple[dict[str, float], dict[str, str], list[str], list[str]]:
    """解析 judge 的 JSON 输出，返回 (维度分, 证据, critical_issues, 警告)。

    容错点（模型常犯）：
      - markdown 代码围栏包裹
      - 分数写成字符串
      - 少给某个维度
      - total 字段缺失或算错（本地重算，不信模型）
    """
    warnings: list[str] = []
    raw = (text or "").strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, flags=re.DOTALL)
    if m:
        raw = m.group(1)
    data: dict[str, Any] | None = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        for i, ch in enumerate(raw):
            if ch in "{[":
                try:
                    data = json.loads(raw[i:])
                    break
                except json.JSONDecodeError:
                    continue
    if not isinstance(data, dict):
        raise ValueError(f"无法解析 judge 输出：{raw[:200]}")

    scores_raw = data.get("scores")
    if not isinstance(scores_raw, dict):
        raise ValueError("judge 输出缺少 scores 字段")

    dims: dict[str, float] = {}
    evidence: dict[str, str] = {}
    na_dims: set[str] = set()
    for key in _DIM_WEIGHTS:
        item = scores_raw.get(key)
        if not isinstance(item, dict):
            warnings.append(f"judge 缺少维度 {key}")
            continue
        if item.get("not_applicable") is True:
            na_dims.add(key)
            continue
        try:
            dims[key] = max(0.0, min(100.0, float(item.get("score", 0))))
        except (TypeError, ValueError):
            warnings.append(f"judge 维度 {key} 分数非法")
        ev = item.get("evidence")
        if isinstance(ev, str) and ev.strip():
            evidence[key] = ev.strip()[:120]

    if not dims:
        raise ValueError("judge 没有给出任何有效维度分")

    # 本地重算总分：不信模型自己加的 total（它常算错或漏算归一化）
    weight_sum = sum(_DIM_WEIGHTS[k] for k in dims)
    if weight_sum <= 0:
        raise ValueError("judge 维度权重为零")
    total = sum(dims[k] * _DIM_WEIGHTS[k] for k in dims) / weight_sum

    issues = data.get("critical_issues")
    issues_list = [str(x)[:60] for x in issues] if isinstance(issues, list) else []
    return dims, evidence, issues_list, warnings


# ---------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------


class EvalRunner:
    """跑 eval 的引擎。"""

    def __init__(
        self,
        llm: LLM,
        *,
        provider: str = DEFAULT_PROVIDER,
        provider_label: str = DEFAULT_PROVIDER_LABEL,
        max_duration: float = DEFAULT_MAX_DURATION,
        use_judge: bool = True,
        judge_max_shots: int = JUDGE_MAX_SHOTS,
        retry_on_critical: bool = True,
    ) -> None:
        self.llm = llm
        self.provider = provider
        self.provider_label = provider_label
        self.max_duration = max_duration
        self.use_judge = use_judge
        self.judge_max_shots = judge_max_shots
        self.retry_on_critical = retry_on_critical

    # ---- 单条 case ----

    def run_case(self, case: EvalCase) -> CaseResult:
        """跑一条 case。

        出现 critical 时**复跑一次**再判定：LLM 输出有抖动，
        单次出现的 critical 可能是偶发而非稳定缺陷。
        两次都失败才算真问题——否则 CI 会被随机失败磨平。

        链路异常（导演拆解失败等）同样复跑一次：2026-11-12 实测
        single_pressure 被一次 deepseek 调用异常打红，直接复现就通过——
        基建抖动不该算质量缺陷（blocked 的除外，那是环境问题不是抖动）。
        """
        res = self._run_case_once(case)
        if self.retry_on_critical and not res.ok and not res.blocked:
            retry = self._run_case_once(case)
            if retry.ok:
                retry.metadata_note = "首次链路异常，复跑通过（判为基建抖动，不计入失败）"
                return retry
            retry.metadata_note = f"两次运行链路都失败（首次：{res.error}），判为稳定缺陷"
            return retry
        if self.retry_on_critical and res.ok and res.critical_count:
            retry = self._run_case_once(case)
            if retry.critical_count == 0:
                retry.metadata_note = "首次出现 critical，复跑后消失（判为抖动，不计入失败）"
                return retry
            # 复跑仍有 critical → 判为稳定缺陷。保留复跑结果（它同样携带失败明细），
            # 并把首次的失败条数一并记下，便于判断严重程度。
            retry.metadata_note = (
                f"两次运行都出现 critical（首次 {res.critical_count} 条 / "
                f"复跑 {retry.critical_count} 条），判为稳定缺陷"
            )
            return retry
        return res

    def _run_case_once(self, case: EvalCase) -> CaseResult:
        res = CaseResult(case=case)
        try:
            script = case.script

            # ① 导演拆解——必须与线上同参调用：不传 adaptation_type 时
            #    片长预算=None，确定性闸门与短剧节奏约束整个在 eval 里失效
            #    （2026-11-12 evals 重设计：eval 与线上用同一套规则，否则
            #    eval 绿了线上照样拆出 53 镜）。
            d = run_director(self.llm, script, adaptation_type="short_drama")
            if not d.success or not isinstance(d.data, list):
                res.error = f"导演拆解失败：{d.summary or d.errors or '未知原因'}"
                _mark_blocked(res)
                return res
            res.breakdowns = d.data

            # ② 美术指导（视觉锚的单一来源）
            art = run_art_director(self.llm, script, language="zh")
            if art.success and isinstance(art.data, StyleGuide):
                res.style_guide = art.data

            # ③ 摄影指导
            dp = run_dp(
                self.llm,
                script,
                breakdowns=res.breakdowns,
                style_guide=res.style_guide,
                target_provider=self.provider,
                provider_label=self.provider_label,
                max_duration=self.max_duration,
            )
            if not dp.success or not isinstance(dp.data, list):
                res.error = f"摄影指导失败：{dp.summary or dp.errors or '未知原因'}"
                _mark_blocked(res)
                return res
            res.shots = dp.data

            # ④ 确定性断言
            res.assertions = [
                assert_director(res.breakdowns, script=script),
                assert_dp(res.shots, script=script, style_guide=res.style_guide),
                assert_pipeline(res.breakdowns, res.shots, script=script, style_guide=res.style_guide),
            ]

            # ⑤ judge（可选）
            if self.use_judge:
                res.judge_scores = self._judge_case(case, res)

            return res

        except Exception as e:  # noqa: BLE001
            log.exception("eval case 执行失败：%s", case.id)
            reason = _classify_block(e)
            if reason:
                res.blocked = True
                res.blocked_reason = reason
                res.error = f"被基础设施挡住：{reason}"
            else:
                res.ok = False
                res.error = f"{type(e).__name__}: {e}"
            return res

    # ---- judge ----

    def _judge_case(self, case: EvalCase, res: CaseResult) -> list[JudgeScore]:
        scores: list[JudgeScore] = []
        # 均匀取样：每个 case 最多 judge 几镜，避免长 case 成本失控
        shots = res.shots
        if not shots:
            return scores
        step = max(1, len(shots) // self.judge_max_shots)
        picked = shots[::step][: self.judge_max_shots]

        for shot in picked:
            score = JudgeScore(shot_id=shot.id)
            try:
                user = _build_judge_user(case, res, shot)
                resp = self.llm.chat().invoke(
                    [
                        {"role": "system", "content": JUDGE_SYSTEM},
                        {"role": "user", "content": user},
                    ]
                )
                dims, evidence, issues, _warn = parse_judge_output(str(resp.content or ""))
                score.dims = dims
                score.evidence = evidence
                score.critical_issues = issues
                weight_sum = sum(_DIM_WEIGHTS[k] for k in dims)
                score.total = sum(dims[k] * _DIM_WEIGHTS[k] for k in dims) / weight_sum
            except Exception as e:  # noqa: BLE001
                score.error = f"{type(e).__name__}: {e}"
            scores.append(score)
        return scores

    # ---- 全量 ----

    def run_all(self, cases: list[EvalCase]) -> EvalReport:
        report = EvalReport(provider=self.provider, model=self._model_name())
        for case in cases:
            r = self.run_case(case)
            report.results.append(r)
            if r.blocked:
                status = "SKIP"
            elif r.ok and r.critical_count == 0:
                status = "OK"
            else:
                status = "FAIL"
            # 分数是五维加权平均，直接打到控制台会变成 78.64999999999999
            judge_cell = f"{r.judge_total:.1f}" if r.judge_total is not None else "—"
            print(
                f"  [{status}] {case.id:<20} shots={len(r.shots):<3} "
                f"critical={r.critical_count:<3} warn={r.warn_count:<3} "
                f"judge={judge_cell}",
                flush=True,
            )
        return report

    def _model_name(self) -> str:
        try:
            return self.llm.describe().get("model", "")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return ""


def _build_judge_user(case: EvalCase, res: CaseResult, shot: Shot) -> str:
    """组装送给 judge 的用户消息。"""
    parts: list[str] = []
    parts.append("## 剧本梗概\n" + case.script.logline)

    anchors = []
    if res.style_guide:
        for name, appear in (res.style_guide.character_appearances or {}).items():
            anchors.append(f"人物「{name}」的既定造型：{appear}")
        for name, env in (res.style_guide.environment_descriptions or {}).items():
            anchors.append(f"地点「{name}」的既定描述：{env}")
    if anchors:
        parts.append("## 前序信息（视觉锚，单一事实来源）\n" + "\n".join(anchors))

    parts.append(
        "## 待评审内容\n"
        f"镜头 id：{shot.id}\n"
        f"景别：{shot.shot_type}\n"
        f"主体：{shot.subject}\n"
        f"动作：{shot.action}\n"
        f"光线：{shot.lighting or '(空)'}\n"
        f"空间关系：{shot.spatial or '(空)'}\n"
        f"背景人物动作：{shot.background_action or '(空)'}\n"
        f"运镜：{shot.camera.type} / 轨迹：{shot.camera.path or '(空)'} / 机位：{shot.camera.height or '(空)'}\n"
        f"镜头功能：{shot.function or '(空)'}\n"
        f"最终画面：{shot.final_frame or '(空)'}\n"
        f"切镜理由：{shot.cut_reason or '(空)'}\n"
        f"时长：{shot.duration_sec}s\n\n"
        f"最终交给视频模型的提示词：\n{shot.video_prompt or '(空)'}"
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------
# 汇总工具（给 pytest 用）
# ---------------------------------------------------------------------


def build_report(llm: LLM, cases: list[EvalCase], **kwargs: Any) -> EvalReport:
    runner = EvalRunner(llm, **kwargs)
    return runner.run_all(cases)


def build_eval_llm(settings: Any = None, *, temperature: float = 0.0) -> LLM:
    """构造 eval 专用的模型门面。

    **必须把温度钉到 0**：默认温度 0.4 下，同一份代码两次跑出的分镜与提示词
    并不相同——回归信号会被抖动淹没。0 温度（贪心解码）能把它压到可比的水平
    （注意：远端模型仍不保证逐字一致，必要时用「critical 复跑一次」兜底）。
    """
    from app.llm import build_llm

    # D-48 后模型配置只走 DB/BYOK：store 传 None 会拿到空配置，
    # eval 与产品必须同源，故显式接全局 store。
    from app.deps import store as get_store

    return build_llm(settings, get_store(), temperature=temperature)


def probe_llm(llm: LLM) -> str:
    """探活：发一条极短的请求，确认模型真的能调通。

    返回空字符串表示可用；否则返回阻塞原因（余额不足 / 限流 / 鉴权失败 / 连不上）。
    配置存在不等于能调用——余额耗尽时 `llm.available` 仍然是 True。
    """
    if not llm.available:
        return "未配置对话模型"
    try:
        resp = llm.chat().invoke([{"role": "user", "content": "回复两个字：可用"}])
        if not str(resp.content or "").strip():
            return "模型返回空内容"
    except Exception as e:  # noqa: BLE001
        reason = _classify_block(e)
        return reason or f"{type(e).__name__}: {e}"[:200]
    return ""


def overall_summary(results: list[AssertionResult]) -> dict[str, Any]:
    return summarize(results)
