# =====================================================================
# test_script_doctor.py —— 剧本医生：确定性体检（2026-11-20，C3）
#
# 对标即梦「剧本医生综合诊断报告」（质量分/等级/送审建议/问题清单）。
# 我们的版本全部是确定性规则：复用 validate_script、story_plan 事件清单、
# rhythm 口播秒数 + 钩子/过审词表——零 LLM、零花费、结果可复跑。
# =====================================================================

from __future__ import annotations

from app.crew.script_doctor import doctor_script
from app.domain import (
    Character,
    Location,
    Scene,
    Script,
    ScriptBeat,
    Source,
)
from app.pipeline.story_plan import EpisodeArc, StoryEvent, StoryPlan, StoryUnderstanding


def _script(beats: list[ScriptBeat], title: str = "诞生", **sc_kw) -> Script:
    sc = Scene(id="scene_001", title=title, chapter_refs=["ch_001"],
               location_id="loc_001", characters=["char_001"],
               purpose="建立控制关系", conflict="服从与觉醒", time="夜", beats=beats, **sc_kw)
    return Script(
        title="替身", version="1.0", language="zh-CN", logline="克隆体取代本体。",
        source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[Character(id="char_001", name="林远")],
        locations=[Location(id="loc_001", name="书房")],
        scenes=[sc],
    )


def _good_script() -> Script:
    return _script([
        ScriptBeat(id="beat_001", type="dialogue", speaker="char_001", line="从今天起你叫β。"),
        ScriptBeat(id="beat_002", type="action", text="林远把衣服丢到舱前，β起身接过。"),
    ])


def _plan(events: list[tuple[str, str]]) -> StoryPlan:
    return StoryPlan(
        understanding=StoryUnderstanding(
            events=[StoryEvent(id=f"ev_{i:03d}", text=t, episode=e)
                    for i, (t, e) in enumerate(events, 1)]),
        episodes=[EpisodeArc(index=1, title="第一集")],
    )


# ---------------------------------------------------------------- 干净剧本


def test_合格剧本满分可进制作():
    rep = doctor_script(_good_script(), _plan([("林远把衣服丢到舱前", "1")]))
    assert rep.score == 100 and rep.grade == "A"
    assert rep.submission == "pass"
    assert not rep.issues


# ---------------------------------------------------------------- 结构


def test_空节拍场判error():
    rep = doctor_script(_script([]))
    assert rep.submission == "needs_review"
    assert any(i.severity == "error" and "没有" in i.text and "节拍" in i.text for i in rep.issues)


def test_兜底目的亮黄灯():
    s = _good_script()
    s.scenes[0].purpose = "推进情节。"
    rep = doctor_script(s)
    assert any(i.severity == "warn" and "目的" in i.text for i in rep.issues)


# ---------------------------------------------------------------- 台词


def test_无说话人台词判error():
    s = _script([ScriptBeat(id="beat_001", type="dialogue", speaker=None, line="你来了。")])
    rep = doctor_script(s)
    assert any(i.severity == "error" and "说话人" in i.text for i in rep.issues)


def test_超长对白场亮黄灯():
    long_line = "这是一段非常长的台词" * 12          # ~108 字 ≈ 28.5s+
    s = _script([
        ScriptBeat(id="beat_001", type="dialogue", speaker="char_001", line=long_line),
        ScriptBeat(id="beat_002", type="dialogue", speaker="char_001", line=long_line),
    ])
    rep = doctor_script(s)
    assert any(i.severity == "warn" and "口播" in i.text for i in rep.issues)


# ---------------------------------------------------------------- 事件覆盖


def test_提案事件写丢判error():
    s = _script([
        ScriptBeat(id="beat_001", type="action", text="林远造出替身β，β接过营养液。"),
    ])
    rep = doctor_script(s, _plan([
        ("林远造出替身β", "1"),
        ("β第一次品尝黑森林蛋糕", "1"),      # 剧本里没有
    ]))
    errs = [i for i in rep.issues if i.severity == "error" and i.area == "事件"]
    assert len(errs) == 1 and "黑森林蛋糕" in errs[0].text


# ---------------------------------------------------------------- 钩子与过审


def test_开场无钩子亮黄灯():
    s = _script([
        ScriptBeat(id="beat_001", type="action", text="房间里很安静。"),
        ScriptBeat(id="beat_002", type="action", text="他坐着。"),
    ])
    rep = doctor_script(s)
    assert any(i.severity == "warn" and "钩子" in i.text for i in rep.issues)


def test_位移级动作或对白算钩子():
    rep = doctor_script(_good_script())          # beat_001 有对白
    assert not any("钩子" in i.text for i in rep.issues)


def test_过审直陈词亮黄灯并给暗示式建议():
    s = _script([
        ScriptBeat(id="beat_001", type="action", text="他掏出注射器扎了下去，血迹斑斑。"),
        ScriptBeat(id="beat_002", type="dialogue", speaker="char_001", line="起效了。"),
    ])
    rep = doctor_script(s)
    safety = [i for i in rep.issues if i.area == "过审"]
    assert len(safety) == 2
    assert rep.submission == "pass_with_check"   # 有过审点：建议处理后再进
    assert "暗示式" in safety[0].text


# ---------------------------------------------------------------- 报告形态


def test_报告markdown带分数与建议():
    rep = doctor_script(_script([]))
    md = rep.to_markdown("替身")
    assert "# 剧本医生体检报告：替身" in md
    assert "质量分" in md and "送审/进制作建议" in md
    assert "## 必须修" in md


# ---------------------------------------------------------------- 工具层


def test_工具_体检报告进对话(store):
    from app.chat.tools import build_chat_tools

    collector = {"project_id": None, "payloads": []}

    class _GenLLM:
        available = True
        def system_prompt(self): return "s"
        def structured(self, schema):
            def _inv(msgs):
                from app.pipeline.generation import Bible, ScenePlan
                from app.pipeline.story_plan import StoryPlan as SP
                if schema is SP:
                    return StoryPlan(understanding=StoryUnderstanding(
                        events=[StoryEvent(id="ev_001", text="β尝到蛋糕", episode=1)]),
                        episodes=[EpisodeArc(index=1, title="第一集")])
                if schema is Bible:
                    return Bible(title="替身", logline="l",
                                 characters=[{"name": "林远", "id": "char_001"}],
                                 locations=[{"name": "书房"}])
                return ScenePlan.model_validate({"scenes": [{
                    "scene_id": "scene_001", "title": "诞生", "location": "书房",
                    "characters": ["林远"], "time": "夜",
                    "beats": [
                        {"type": "dialogue", "speaker": "char_001", "line": "从今天起你叫β。", "id": "beat_001"},
                        {"type": "action", "text": "β起身接过衣服。", "id": "beat_002"},
                    ]}]})
            return type("_R", (), {"invoke": staticmethod(_inv)})()

    tools = {t.name: t for t in build_chat_tools(store, _GenLLM(), collector)}
    tools["create_project"].invoke({"title": "替身", "raw_text": "林远造出替身β。β尝到蛋糕。"})
    pid = collector["project_id"]
    tools["generate_script"].invoke({"project_id": pid})

    r = tools["run_script_doctor"].invoke({"project_id": pid})
    assert "质量分" in r and ("pass" in r or "needs review" in r)

    # 没剧本时如实说没有
    collector2 = {"project_id": None, "payloads": []}
    tools2 = {t.name: t for t in build_chat_tools(store, _GenLLM(), collector2)}
    tools2["create_project"].invoke({"title": "空", "raw_text": "一句话。"})
    r2 = tools2["run_script_doctor"].invoke({"project_id": collector2["project_id"]})
    assert "还没有剧本" in r2
