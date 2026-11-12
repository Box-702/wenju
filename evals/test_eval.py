# =====================================================================
# test_eval.py —— 提示词质量回归的 pytest 入口
#
# 与 tests/ 的分工：
#   tests/  单元回归——测「代码不崩」
#   evals/  质量回归——测「提示词质量没退化」
#
# 三层：
#   L0 纯离线：不需要模型、不需要 key。跑解析器与断言库的自身逻辑。
#   L1 需要 key：跑真实链路 + 确定性断言（critical 必须为 0）。
#   L2 需要 key：额外跑 LLM judge 打分（看趋势，不参与红绿判定）。
#
# 没有配置对话模型时，L1/L2 自动 skip——CI 不配 secret 也能跑。
# =====================================================================

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.llm import LLM
from app.config import Settings
from app.domain import CameraMovement, SceneBreakdown, Shot, StyleGuide

from .assertions import assert_director, assert_dp, assert_pipeline
from .cases import ALL_CASES, get_case
from .runner import EvalRunner, parse_judge_output

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------


@pytest.fixture(scope="session")
def eval_llm() -> LLM:
    """真实对话模型（温度钉 0，保证可复现）。

    配置存在不等于能调用——余额耗尽时 `llm.available` 仍是 True，
    所以必须**探活一次**，否则整层测试会以「质量回归」的假象失败。
    """
    from .runner import build_eval_llm, probe_llm

    settings = Settings()
    try:
        llm = build_eval_llm(settings)
    except Exception:  # noqa: BLE001
        pytest.skip("无法构造对话模型，跳过提示词质量 eval")
    if not llm.available:
        pytest.skip("未配置可用的对话模型，跳过提示词质量 eval")

    reason = probe_llm(llm)
    if reason:
        pytest.skip(f"对话模型不可用（{reason}），跳过提示词质量 eval 的在线层")
    return llm


# =====================================================================
# L0 —— 纯离线层（无 key 可跑）
# =====================================================================


def test_all_cases_are_wellformed():
    """每条 case 的剧本必须能被 domain 模型接受，且有真实内容。"""
    assert len(ALL_CASES) >= 10, "golden case 至少要 10 条（Anthropic 的建议下限）"
    for case in ALL_CASES:
        sc = case.script
        assert sc.scenes, f"{case.id}: 没有场景"
        assert sc.logline.strip(), f"{case.id}: 没有梗概"
        assert sc.characters, f"{case.id}: 没有人物"
        # 场景必须有真实冲突，不是占位
        for s in sc.scenes:
            assert s.purpose.strip(), f"{case.id}/{s.id}: purpose 为空"
            assert s.conflict.strip(), f"{case.id}/{s.id}: conflict 为空"


def test_case_ids_unique():
    ids = [c.id for c in ALL_CASES]
    assert len(ids) == len(set(ids))


def test_parse_judge_output_handles_code_fence():
    """judge 输出常被 markdown 围栏裹住，解析器必须能处理。"""
    raw = """```json
{"scores": {"dramatic": {"score": 88, "evidence": "他把手放下"},
 "renderable": {"score": 80, "evidence": ""},
 "consistency": {"score": 70, "evidence": "", "not_applicable": false},
 "specificity": {"score": 90, "evidence": ""},
 "restraint": {"score": 85, "evidence": ""}},
 "total": 83, "critical_issues": []}
```"""
    dims, evidence, issues, warns = parse_judge_output(raw)
    assert dims["dramatic"] == 88.0
    assert dims["consistency"] == 70.0
    assert evidence["dramatic"] == "他把手放下"
    assert issues == []
    assert warns == []


def test_parse_judge_output_recomputes_total():
    """模型自己加的 total 常算错——必须以本地重算为准。"""
    raw = """{"scores": {"dramatic": {"score": 100}, "renderable": {"score": 100},
     "consistency": {"score": 100}, "specificity": {"score": 100}, "restraint": {"score": 100}},
     "total": 3}"""
    dims, _, _, _ = parse_judge_output(raw)
    weight_sum = 0.25 + 0.25 + 0.20 + 0.15 + 0.15
    expected = sum(dims[k] * w for k, w in
                   {"dramatic": .25, "renderable": .25, "consistency": .20,
                    "specificity": .15, "restraint": .15}.items()) / weight_sum
    assert expected == 100.0


def test_parse_judge_output_handles_string_scores():
    raw = """{"scores": {"dramatic": {"score": "77"}, "renderable": {"score": 70},
     "consistency": {"score": 60, "not_applicable": true}, "specificity": {"score": 80},
     "restraint": {"score": 90}}, "total": 0}"""
    dims, _, _, warns = parse_judge_output(raw)
    assert dims["dramatic"] == 77.0
    assert "consistency" not in dims  # not_applicable 的维度不参与
    assert warns == []


def test_parse_judge_output_tolerates_missing_dimension():
    raw = """{"scores": {"dramatic": {"score": 80}, "renderable": {"score": 80}},
     "total": 80}"""
    dims, _, _, warns = parse_judge_output(raw)
    assert len(dims) == 2
    assert any("specificity" in w for w in warns)


def test_parse_judge_output_rejects_garbage():
    with pytest.raises(ValueError):
        parse_judge_output("这不是 JSON")


def test_assertions_flag_known_bad_output():
    """断言库本身必须能抓到已知的坏输出——否则它对真实输出也没用。"""
    case = get_case("single_pressure")
    res = assert_dp([], script=case.script)
    assert not res.passed
    assert any(f.rule == "DPT-00" for f in res.failures)


def test_assertions_report_missing_scene_coverage():
    """剧本里的戏没被拆到时，跨链路断言必须报出来。"""
    case = get_case("single_pressure")
    res = assert_pipeline([], [], script=case.script)
    rules = {f.rule for f in res.failures}
    assert "LNG-03" in rules, f"没拆到的场景应报 LNG-03，实际报了：{rules}"


def test_assertions_flag_slop_and_negation_in_prompt():
    """断言库必须能抓到空洞词与否定句——这是提示词最常见的两种硬伤。"""
    from app.domain import Shot

    shot = Shot(
        id="shot_scene_001_001",
        scene_id="scene_001",
        order=0,
        subject="一个人",
        action="他站着不动",
        video_prompt="电影感十足的光影，没有任何人出现，极具震撼感的氛围感场景",
    )
    res = assert_dp([shot])
    rules = {f.rule for f in res.failures}
    assert "DPT-03" in rules, f"空洞词应报 DPT-03，实际：{rules}"
    assert "DPT-04" in rules, f"否定句应报 DPT-04，实际：{rules}"


# =====================================================================
# L0.5 —— 离线回归层：拿录制的真实产出跑断言（不需要模型、不需要 key）
# =====================================================================


def _load_fixture(name: str) -> dict:
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        pytest.skip(f"缺少 fixture：{path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixture_real_prompt_passes_production_audit():
    """真实链路产出的提示词，必须能干净通过生产审计器。

    这条锁的是「不误报」——实测中修复前的审计器对真实提示词 4/4 全误报
    「一镜多运镜」，用户每审一镜都要被一条假告警打扰。
    """
    from app.crew.dramaturgy import audit_video_prompt

    fx = _load_fixture("dialogue_tension")
    style = fx["style_guide"].get("camera_style") or ""
    for shot in fx["shots"]:
        issues = audit_video_prompt({"video_prompt": shot["video_prompt"]}, style_context=style)
        assert not issues, f"{shot['id']} 出现不该有的告警：{issues}"


def test_fixture_real_prompt_passes_eval_assertions():
    """真实产出在 eval 断言层也不应该有 critical。"""
    fx = _load_fixture("dialogue_tension")
    case = get_case(fx["case_id"])
    style_guide = StyleGuide(**fx["style_guide"])
    shots = [Shot(**s) for s in fx["shots"]]

    res = assert_dp(shots, script=case.script, style_guide=style_guide)
    criticals = res.critical
    assert not criticals, f"真实产出不应有 critical：{[str(c) for c in criticals]}"


def test_fixture_anchor_is_reused_verbatim():
    """视觉锚必须逐字复用——这是跨镜一致性的第一道保障。"""
    fx = _load_fixture("dialogue_tension")
    style = fx["style_guide"]
    for shot in fx["shots"]:
        prompt = shot["video_prompt"]
        for name, appear in (style.get("character_appearances") or {}).items():
            if f"{name}：" in prompt:
                assert appear in prompt, f"{shot['id']} 里「{name}」的造型没有逐字复用"


# =====================================================================
# L1 —— 真实链路 + 确定性断言（需要 key）
# =====================================================================


@pytest.mark.parametrize("case_id", [c.id for c in ALL_CASES])
def test_case_deterministic_assertions(eval_llm: LLM, case_id: str):
    """每条 golden case 跑完整链路，确定性层不允许出现 critical。

    这是 eval 的硬红线：出现 critical 说明这次改动引入了会让出片失败的缺陷。
    """
    case = get_case(case_id)
    runner = EvalRunner(eval_llm, use_judge=False)
    res = runner.run_case(case)

    if res.blocked:
        pytest.skip(f"{case_id} 被基础设施挡住：{res.blocked_reason}")
    assert res.ok, f"{case_id} 链路执行失败：{res.error}"
    assert res.breakdowns, f"{case_id} 导演没有产出分镜"
    assert res.shots, f"{case_id} 摄影指导没有产出镜头"

    criticals = [f for r in res.assertions for f in r.critical]
    assert not criticals, (
        f"{case_id} 出现 {len(criticals)} 条 critical：\n"
        + "\n".join(f"  {f}" for f in criticals[:12])
    )


# =====================================================================
# L2 —— LLM judge 打分（需要 key；不参与红绿判定）
# =====================================================================


@pytest.mark.parametrize("case_id", [c.id for c in ALL_CASES])
def test_case_judge_scores(eval_llm: LLM, case_id: str):
    """跑 judge 打分。这里只断言 judge 本身能跑通、分数在合理区间。

    **不设分数下限门禁**——LLM 打分有抖动，样本量（10 条）也不够做统计判定。
    分数趋势由 runner 的报告负责呈现，改动前后对比看。
    """
    case = get_case(case_id)
    runner = EvalRunner(eval_llm, use_judge=True)
    res = runner.run_case(case)

    if res.blocked:
        pytest.skip(f"{case_id} 被基础设施挡住：{res.blocked_reason}")
    assert res.ok, f"{case_id} 链路执行失败：{res.error}"
    scored = [s for s in res.judge_scores if not s.error]
    assert scored, f"{case_id} judge 一个都没跑成功：{[s.error for s in res.judge_scores]}"

    for s in scored:
        assert 0 <= s.total <= 100, f"{case_id}/{s.shot_id} 总分越界：{s.total}"
        assert s.dims, f"{case_id}/{s.shot_id} 没有维度分"


def test_report_renders(eval_llm: LLM):
    """报告能渲染、JSON 能序列化——不然结果没法进 CI artifact。"""
    runner = EvalRunner(eval_llm, use_judge=False)
    report = runner.run_all(ALL_CASES[:2])
    text = report.render()
    assert "提示词质量 eval 报告" in text
    assert report.to_dict()["cases"] == 2


def test_blocked_case_is_not_counted_as_failure():
    """余额耗尽是基础设施问题，不能算质量回归——但必须显式标出来。"""
    from .runner import CaseResult, EvalReport

    res = CaseResult(case=get_case("single_pressure"))
    res.blocked = True
    res.blocked_reason = "402 Insufficient Balance"
    res.ok = False

    report = EvalReport(results=[res])
    assert report.blocked_total == 1
    assert report.failed_total == 0
    # 报告里必须显式列出 blocked，否则「跑不动」会被误读成「跑过了且通过」
    assert "被基础设施挡住" in report.render()
    assert report.to_dict()["blocked_total"] == 1


def test_assertions_flag_light_source_swap():
    """光源被换掉要报 DPT-13：分镜写窗光，提示词写台灯，同一场就忽明忽暗。

    2026-10-30 的 judge 基线里这类最伤连贯性（chain_action 37.5 / night_exterior 55）：
    单镜都通顺，连起来人物脸光全在跳。
    """
    shot = Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0,
        subject="梁述", action="他站在床边", lighting="右侧窗户午后斜阳",
        video_prompt="床头小夜灯是唯一主光，暖黄的光在他脸上压出一道硬边，"
                     "他慢慢解开袖扣，影子在墙上拉得很长，手指在发抖。",
    )
    res = assert_dp([shot])
    assert "DPT-13" in {f.rule for f in res.failures}, [str(f) for f in res.failures]


def test_assertions_flag_light_direction_flip():
    """光位从左边翻到右边要报 DPT-14——这才是「光被重写」而不是「光被补全」。"""
    shot = Shot(
        id="shot_scene_001_001", scene_id="scene_001", order=0,
        subject="梁述", action="他靠在栏杆上", lighting="洋红主光从左前方打进来",
        video_prompt="粉绿的冷光从右后方扫过来，他侧过脸，指节抵着栏杆慢慢泛白，"
                     "身后的霓虹在积水里碎成一片。",
    )
    res = assert_dp([shot])
    assert "DPT-14" in {f.rule for f in res.failures}, [str(f) for f in res.failures]


def test_assertions_flag_repeated_clause():
    """同一句写两遍要报 DPT-15（拼装层重复追加的典型症状）。"""
    dup = "巷口第一缕阳光落在湿漉漉的青石板上"
    shot = Shot(
        id="shot_scene_001_002", scene_id="scene_001", order=0,
        subject="老周", action="他推开门", lighting="清晨的窗光",
        video_prompt=f"门被推开，{dup}。他站着没动，手指还搭在门框上。{dup}。",
    )
    res = assert_dp([shot])
    assert "DPT-15" in {f.rule for f in res.failures}, [str(f) for f in res.failures]


def test_production_audit_reports_repeated_clause():
    """审阅时也要看得见：生产审计器和 eval 共用同一份检测，别只在一个层修。"""
    from app.crew.dramaturgy import audit_video_prompt

    dup = "雨停之后巷口的第一缕阳光落下来"
    issues = audit_video_prompt(
        {"video_prompt": f"他推开门，{dup}。他站着没动，手指还搭在门框上。{dup}。"}
    )
    assert any("同一句写了两遍" in s for s in issues), issues


def test_production_audit_keeps_legit_refrain():
    """「他推门。门开了。」这种正当回环不能被判成重复。"""
    from app.crew.dramaturgy import audit_video_prompt

    issues = audit_video_prompt(
        {"video_prompt": "他抬手推门。门开了。走廊尽头的灯忽明忽暗，"
                         "他的影子被拉长又缩短，像有人在原地踱步。"}
    )
    assert not any("同一句写了两遍" in s for s in issues), issues


def test_report_verdict_is_three_state():
    """一条都没跑起来时不能报「通过」——余额耗尽那天 CI 会亮假绿灯。"""
    from .runner import CaseResult, EvalReport

    case = get_case("single_pressure")
    r_all_blocked = EvalReport(provider="minimax", model="m")
    r_all_blocked.results = [
        CaseResult(case=case, ok=False, blocked=True, blocked_reason="Insufficient Balance")
    ]
    assert "无法评估" in r_all_blocked.render()

    r_partial = EvalReport(provider="minimax", model="m")
    ok = CaseResult(case=case, ok=True)
    blocked = CaseResult(case=case, ok=False, blocked=True, blocked_reason="402")
    r_partial.results = [ok, blocked]
    assert "有 1 条未评估" in r_partial.render()

    r_clean = EvalReport(provider="minimax", model="m")
    r_clean.results = [CaseResult(case=case, ok=True)]
    assert r_clean.render().rstrip().endswith("结论：通过")


def test_pipeline_flags_light_conflict_between_roles():
    """美术指导与导演对同一空间的光各定一次、且互相矛盾时，必须报 LNG-04。

    这条不是构造的例子：`dialogue_tension` 的录制样本里美术指导写「钨丝灯泡、暖黄」，
    导演分镜写「冷白日光灯管」。提示词照抄了环境锚，所以锚复用检查全绿——
    两层物理设定打架这件事，只有跨链路能看见。
    """
    fx = _load_fixture("dialogue_tension")
    shots = [Shot(**s) for s in fx["shots"]]
    res = assert_pipeline([], shots, style_guide=StyleGuide(**fx["style_guide"]))
    assert "LNG-04" in {f.rule for f in res.failures}, [str(f) for f in res.failures]


def test_pipeline_light_check_silent_when_consistent():
    """光源同属一类时不报——否则每一条都会亮黄灯，审阅者很快就不看了。"""
    sg = StyleGuide(lighting_style="头顶一盏裸露的钨丝灯泡，暖黄，桌面亮出一圈光斑")
    shot = Shot(
        id="shot_scene_001_000", scene_id="scene_001", order=0,
        subject="何清", action="他把钱推过去",
        lighting="钨丝灯泡从头顶偏后打下，桌面亮出一圈暖黄",
        video_prompt="钨丝灯泡从头顶偏后打下，桌面亮出一圈暖黄，他把钱推过去，手指在桌沿停住。",
    )
    res = assert_pipeline([], [shot], style_guide=sg)
    assert "LNG-04" not in {f.rule for f in res.failures}, [str(f) for f in res.failures]


# ---- 全片层镜头语言/动作密度断言（2026-11-12 evals 重设计，DIR-10~13） ----


def _flat_shot(i: int, *, st: str, cam: str, action: str) -> Shot:
    return Shot(
        id=f"shot_scene_001_{i:03d}", scene_id="scene_001", order=i,
        subject="孔乙己", duration_sec=4, shot_type=st,
        camera=CameraMovement(type=cam), function="emotion",
        final_frame="他站着不动", action=action,
    )


def test_director_flags_monotone_lens_language():
    """v2/v3 型退化：全特写+全静态+零 wide+两段微动作 → 四条 critical 全亮。"""
    shots = [_flat_shot(i, st="close_up" if i < 5 else "medium",
                        cam="static", action="他抿嘴；手指收紧")
             for i in range(10)]
    res = assert_director([SceneBreakdown(scene_id="scene_001", shots=shots)])
    rules = {f.rule for f in res.critical}
    assert {"DIR-10", "DIR-11", "DIR-12", "DIR-13"} <= rules, rules


def test_director_lens_rules_silent_when_varied():
    """多样化 + 三段位移动作的计划不该被新规则误伤。"""
    action_ok = "他俯身从怀里摸出钱袋；他把铜钱一枚枚按在柜台上；手收回来退后半步站直"
    shots = [
        _flat_shot(0, st="wide", cam="dolly", action=action_ok),
        *[_flat_shot(i, st="medium", cam="static", action=action_ok) for i in range(1, 6)],
        _flat_shot(6, st="over_shoulder", cam="pan", action=action_ok),
        _flat_shot(7, st="close_up", cam="static", action=action_ok),
    ]
    res = assert_director([SceneBreakdown(scene_id="scene_001", shots=shots)])
    rules = {f.rule for f in res.critical}
    assert not ({"DIR-10", "DIR-11", "DIR-12", "DIR-13"} & rules), rules
