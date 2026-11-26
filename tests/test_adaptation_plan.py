# =====================================================================
# test_adaptation_plan.py —— 段落级改编决策（D-90）
#
# 只测确定性部分：切段、纠偏、场景绑定、覆盖率体检、目标带。
# LLM 调用不在这里测（在线层要花钱，见 evals/）。
# =====================================================================

from __future__ import annotations

from types import SimpleNamespace

from app.crew.director import _runtime_targets
from app.domain import (
    AdaptationPlan,
    Character,
    Location,
    Scene,
    Script,
    ScriptBeat,
    Source,
    SourceSegmentDecision,
)
from app.pipeline.adaptation import (
    _repair,
    bind_scene_targets,
    coverage_issues,
    is_narrative_source,
    plan_brief,
    scene_plan_issues,
    split_source,
)


def _seg(seg_id: str = "seg_001", **kw) -> SourceSegmentDecision:
    return SourceSegmentDecision(id=seg_id, summary="x", **kw)


def _script(beats_per_scene: list[int], *, plan: AdaptationPlan | None = None) -> Script:
    scenes = []
    for i, n in enumerate(beats_per_scene, 1):
        scenes.append(Scene(
            id=f"scene_{i:03d}", title=f"第 {i} 场", chapter_refs=["ch_001"],
            location_id="loc_001", characters=["char_001"], purpose="p", conflict="c",
            beats=[ScriptBeat(id=f"beat_{j:03d}", type="action", text="他站起来")
                   for j in range(1, n + 1)],
        ))
    return Script(
        title="t", version="1.0", language="zh-CN", logline="甲在房间里。",
        source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[Character(id="char_001", name="甲")],
        locations=[Location(id="loc_001", name="房间")],
        scenes=scenes, adaptation_plan=plan,
    )


# ---------- 切段 ----------

def test_split_source_覆盖原文且编号连续():
    text = "第一段。\n\n第二段。\n\n第三段。"
    segs = split_source(text, min_chars=1)
    assert [s["id"] for s in segs] == ["seg_001", "seg_002", "seg_003"]
    assert "".join(s["text"] for s in segs).replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_split_source_长段按句切且不超上限():
    text = "。".join(f"第{i}句" for i in range(30)) + "。"
    segs = split_source(text, max_chars=60, min_chars=10)
    assert segs
    assert all(len(s["text"]) <= 60 for s in segs)


def test_narrative_source_识别剧本抬头():
    assert is_narrative_source("王生在山上砍柴。\n\n他走进院子。") is True
    assert is_narrative_source("第一场\n内景 · 房间 · 夜\n\n甲说话。\n\n第二场\n外景 · 街道 · 日\n") is False


# ---------- 纠偏 ----------

def test_repair_不可删段落不许删除():
    plan = AdaptationPlan(segments=[
        _seg(plot_importance="must_keep", treatment="cut", target_beats=0, target_sec=0),
    ], target_sec=70)
    _repair(plan, [{"id": "seg_001", "text": "王生撞墙", "chars": 4, "anchor": "王生撞墙"}])
    assert plan.segments[0].treatment == "compress"
    assert plan.segments[0].target_beats >= 1


def test_repair_有完整性风险的删除改压缩():
    plan = AdaptationPlan(segments=[
        _seg(plot_importance="optional", treatment="cut",
             completeness_risk="观众不知道口诀的意义", target_sec=0),
    ], target_sec=70)
    _repair(plan, [{"id": "seg_001", "text": "口诀", "chars": 2, "anchor": "口诀"}])
    assert plan.segments[0].treatment == "compress"


def test_repair_没有戏剧理由的扩写降级为保留():
    plan = AdaptationPlan(segments=[
        _seg(function="setup", treatment="expand", target_beats=3, target_sec=15),
    ], target_sec=70)
    _repair(plan, [{"id": "seg_001", "text": "环境", "chars": 2, "anchor": "环境"}])
    assert plan.segments[0].treatment == "keep"


def test_repair_目标秒数夹进单集目标带():
    plan = AdaptationPlan(segments=[
        _seg(treatment="keep", target_beats=1, target_sec=10),
    ], target_sec=10)
    _repair(plan, [{"id": "seg_001", "text": "x", "chars": 1, "anchor": "x"}])
    assert 55.0 <= plan.target_sec <= 95.0
    assert plan.episode_count == 1


def test_repair_漏段补默认决策():
    plan = AdaptationPlan(segments=[], target_sec=75)
    segs = [
        {"id": "seg_001", "text": "甲", "chars": 1, "anchor": "甲"},
        {"id": "seg_002", "text": "乙", "chars": 1, "anchor": "乙"},
    ]
    notes = _repair(plan, segs)
    assert [s.id for s in plan.segments] == ["seg_001", "seg_002"]
    assert all(s.treatment == "keep" for s in plan.segments)
    assert any("补" in n for n in notes)


# ---------- brief / 场景一致性 ----------

def test_plan_brief_带硬规则和段落目标():
    plan = AdaptationPlan(segments=[
        _seg(plot_importance="must_keep", treatment="expand", function="turn",
             character_focus="王生：从自信到心虚", target_beats=3, target_sec=12),
    ], target_sec=70, hook="王生撞墙")
    text = plan_brief(plan)
    assert "source_segments" in text
    assert "must_keep" in text
    assert "王生撞墙" in text
    assert "12" in text


def test_scene_plan_未认领不可删段落报错():
    plan = AdaptationPlan(segments=[
        _seg(plot_importance="must_keep", treatment="keep", target_beats=2, target_sec=20),
    ], target_sec=60)
    scene_plan = SimpleNamespace(scenes=[
        SimpleNamespace(scene_id="scene_001", source_segments=[], target_sec=20, beats=[]),
    ])
    issues = scene_plan_issues(plan, scene_plan)
    assert any("不可删" in i for i in issues)


def test_scene_plan_认领已删除段落报错():
    plan = AdaptationPlan(segments=[
        _seg(plot_importance="optional", treatment="cut", target_beats=0, target_sec=0),
    ], target_sec=60)
    scene_plan = SimpleNamespace(scenes=[
        SimpleNamespace(scene_id="scene_001", source_segments=["seg_001"], target_sec=20, beats=[]),
    ])
    issues = scene_plan_issues(plan, scene_plan)
    assert any("已删除" in i for i in issues)


def test_bind_scene_targets_补漏认领并按目标校准():
    plan = AdaptationPlan(segments=[
        _seg("seg_001", treatment="keep", target_beats=2, target_sec=20),
        _seg("seg_002", treatment="keep", target_beats=4, target_sec=40),
    ], target_sec=60)
    scenes = [
        SimpleNamespace(id="scene_001", beats=[1, 2]),
        SimpleNamespace(id="scene_002", beats=[1, 2, 3, 4]),
    ]
    scene_plan = SimpleNamespace(scenes=[
        SimpleNamespace(scene_id="scene_001", source_segments=["seg_001"], target_sec=20, beats=[]),
        SimpleNamespace(scene_id="scene_002", source_segments=[], target_sec=None, beats=[]),
    ])
    notes = bind_scene_targets(plan, scene_plan, scenes)
    assert plan.scene_segments["scene_001"] == ["seg_001"]
    assert plan.scene_segments["scene_002"] == ["seg_002"]
    assert abs(sum(plan.scene_targets.values()) - 60.0) < 0.2
    assert any("补到" in n for n in notes)


# ---------- 覆盖率 ----------

def test_coverage_必须保住不可删段落():
    plan = AdaptationPlan(segments=[
        _seg(plot_importance="must_keep", treatment="keep", target_beats=2, target_sec=20),
    ], target_sec=60, scene_segments={}, scene_targets={"scene_001": 60})
    script = _script([2], plan=plan)
    issues = coverage_issues(script, plan)
    assert any("不可删" in i for i in issues)


def test_coverage_删除段不许写成节拍():
    plan = AdaptationPlan(segments=[
        _seg(plot_importance="optional", treatment="cut", target_beats=0, target_sec=0),
    ], target_sec=60, scene_segments={"scene_001": ["seg_001"]},
        scene_targets={"scene_001": 60})
    script = _script([2], plan=plan)
    issues = coverage_issues(script, plan)
    assert any("已删除" in i for i in issues)


# ---------- 导演目标带 ----------

def test_runtime_targets_有段落决策时以plan为准():
    plan = AdaptationPlan(segments=[
        _seg("seg_001", treatment="keep", target_beats=2, target_sec=30),
        _seg("seg_002", treatment="keep", target_beats=4, target_sec=40),
    ], target_sec=70, scene_targets={"scene_001": 30, "scene_002": 40})
    script = _script([2, 4], plan=plan)
    lo, target, hi, episodes, targets = _runtime_targets(script, "short_drama")
    assert (lo, target, hi, episodes) == (55.0, 70.0, 95.0, 1)
    assert abs(targets["scene_001"] - 30) < 0.2
    assert abs(targets["scene_002"] - 40) < 0.2


def test_runtime_targets_无计划时按剧本估算落带():
    script = _script([3, 3, 3])
    lo, target, hi, episodes, targets = _runtime_targets(script, "short_drama")
    assert lo <= target <= hi
    assert episodes == 1
    assert abs(sum(targets.values()) - target) < 0.2
