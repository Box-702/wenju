# =====================================================================
# test_script_text.py —— 剧本文本层体检（2026-11-26）
#
# 用户要求：不能只看最后的视频提示词，中间生成的剧本正文也要把关。
# =====================================================================

from __future__ import annotations

from app.domain import (
    Adaptation, AdaptationPlan, Character, Location, Scene, Script, ScriptBeat, Source,
)
from app.pipeline.script_text import audit_script_text


def _script(**kw) -> Script:
    chars = [
        Character(id="char_001", name="王生", role="protagonist",
                  description="世家子弟，剪 paper 如镜", tags="爱吹嘘"),
        Character(id="char_002", name="道士", role="supporting",
                  description="素发垂领，笑曰「汝之惰也，吾早言之矣」"),
    ]
    beats = [
        ScriptBeat(id="beat_001", type="action", text="王生走到道士面前，声音软下来"),
        ScriptBeat(id="beat_002", type="dialogue", speaker="char_001",
                   line="弟子诚心求道，能之。"),
        ScriptBeat(id="beat_003", type="dialogue", speaker="char_002",
                   line="汝之惰也，吾早言之矣。"),
        ScriptBeat(id="beat_004", type="os", speaker="char_001",
                   line="这老道士到底藏了什么本事？"),
        ScriptBeat(id="beat_005", type="card", text="结尾钩子：他回家就撞墙"),
    ]
    scene = Scene(
        id="scene_001", title="拜师", chapter_refs=["ch_001"],
        location_id="loc_001", characters=["char_001"],
        purpose="拜师", conflict="道士嫌他娇惰",
        beats=beats,
        **kw,
    )
    return Script(
        title="t", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        logline="王生求术。", characters=chars,
        locations=[Location(id="loc_001", name="观宇")],
        scenes=[scene], dialogue_style="vernacular",
    )


def test_文本审计抓英文文言不可拍漏人物() -> None:
    issues = audit_script_text(_script())
    joined = "\n".join(issues)
    assert "paper" in joined                      # 英文残留
    assert "文言" in joined                       # 道士小传/台词文言
    assert "声音软下来" in joined                  # 不可拍
    assert "道士" in joined and "characters 没列" in joined
    assert "场头没有时间" in joined
    assert "内景/外景" in joined


def test_文本审计通过干净剧本() -> None:
    s = _script(time="日", interior_exterior="内景")
    # 删掉英文/文言/不可拍/漏人物问题
    s.characters[0] = Character(id="char_001", name="王生", role="protagonist",
                                description="世家子弟，爱吹嘘")
    s.characters[1] = Character(id="char_002", name="道士", role="supporting",
                                description="白发老道士")
    s.scenes[0].characters = ["char_001", "char_002"]
    s.scenes[0].beats[0] = ScriptBeat(id="beat_001", type="action",
                                      text="王生走到道士面前，肩膀塌下来")
    s.scenes[0].beats[2] = ScriptBeat(id="beat_003", type="dialogue",
                                      speaker="char_002", line="我早说你吃不了苦。")
    issues = audit_script_text(s)
    assert issues == []


def test_人物小传引号里的唱词不算文言() -> None:
    s = _script(time="日", interior_exterior="内景")
    s.characters[0] = Character(id="char_001", name="王生", role="protagonist",
                                description="本地王家的七公子，爱吹牛")
    s.characters[1] = Character(
        id="char_002", name="道士", role="supporting",
        description="白发老道士，常哼一句唱词「仙仙乎，而还乎，而幽我于广寒乎」")
    s.scenes[0].characters = ["char_001", "char_002"]
    s.scenes[0].beats[0] = ScriptBeat(id="beat_001", type="action",
                                      text="王生走到道士面前，肩膀塌下来")
    s.scenes[0].beats[2] = ScriptBeat(id="beat_003", type="dialogue",
                                      speaker="char_002", line="我早说你吃不了苦。")
    issues = audit_script_text(s)
    assert not any("人物小传" in i and "文言" in i for i in issues)


def test_台词引号里的唱词不算文言() -> None:
    s = _script(time="日", interior_exterior="内景")
    s.characters[0] = Character(id="char_001", name="王生", role="protagonist",
                                description="本地王家的七公子")
    s.characters[1] = Character(id="char_002", name="道士", role="supporting",
                                description="白发老道士")
    s.scenes[0].characters = ["char_001", "char_002"]
    s.scenes[0].beats[0] = ScriptBeat(id="beat_001", type="action",
                                      text="王生走到道士面前，肩膀塌下来")
    s.scenes[0].beats[2] = ScriptBeat(id="beat_003", type="dialogue",
                                      speaker="char_002",
                                      line="「仙仙乎，而还乎，而幽我于广寒乎？」")
    issues = audit_script_text(s)
    assert not any("台词仍有文言" in i for i in issues)


def test_短剧估算超过95秒要报() -> None:
    s = _script(time="日", interior_exterior="内景")
    s.characters[0] = Character(id="char_001", name="王生", role="protagonist",
                                description="本地王家的七公子")
    s.characters[1] = Character(id="char_002", name="道士", role="supporting",
                                description="白发老道士")
    s.scenes[0].characters = ["char_001", "char_002"]
    s.scenes[0].beats = [
        ScriptBeat(id=f"beat_{i:03d}", type="dialogue", speaker="char_001",
                   line=f"第{i}句：" + "我大老远跑来学本事，您不能就这么打发我走。" * 2)
        for i in range(1, 11)
    ]
    s.adaptation = Adaptation(type="short_drama")
    s.adaptation_plan = AdaptationPlan(target_sec=85, episode_count=1)
    issues = audit_script_text(s)
    assert any("95" in i and "超过" in i for i in issues)
