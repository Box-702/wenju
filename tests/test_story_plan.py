# =====================================================================
# test_story_plan.py —— 故事规划与按集扩写（2026-11-20，D-71）
#
# 对标即梦实操：先「理解卡 + 分集提案」，用户确认后才按集扩写——
# 事件清单是底线（一不许删），薄原文靠加戏做厚，不靠砍事件压时长。
# 旧路线的病：固定 120 秒预算 → 装不下 → 闸门删 16 镜（《替身v2》复盘）。
# =====================================================================

from __future__ import annotations

from app.pipeline.generation import _stage_scenes
from app.pipeline.story_plan import (
    EpisodeArc,
    StoryEvent,
    StoryPlan,
    StoryUnderstanding,
    adaptation_plan_for,
    ensure_plan_budget,
    episode_brief,
    generate_from_plan,
    plan_story,
)
from app.domain import (
    Character, Location, Scene, Script, ScriptBeat, Source,
)


def _plan(n_eps: int = 3, status: str = "proposed") -> StoryPlan:
    return StoryPlan(
        understanding=StoryUnderstanding(
            logline="克隆体取代本体。",
            core_conflict="β 想要林远的人生。",
            events=[
                StoryEvent(id="ev_001", text="β 诞生", episode=1),
                StoryEvent(id="ev_002", text="蛋糕初味", episode=1),
                StoryEvent(id="ev_003", text="强制服从", episode=2),
                StoryEvent(id="ev_004", text="取代与销毁", episode=9),   # 越界
                StoryEvent(id="", text="敲两下破绽", episode=3),          # 缺 id
            ],
        ),
        episodes=[EpisodeArc(index=i, title=f"第{i}集", hook=f"钩子{i}")
                  for i in range(1, n_eps + 1)],
        status=status,
    )


# ---------------------------------------------------------------- 纠偏


def test_normalized_集号连续_事件越界并入末集_id补号():
    sp = _plan().normalized()
    assert [e.index for e in sp.episodes] == [1, 2, 3]
    ev4 = next(e for e in sp.understanding.events if "销毁" in e.text)
    assert ev4.episode == 3                       # 越界的 9 并入末集
    ev5 = next(e for e in sp.understanding.events if "破绽" in e.text)
    assert ev5.id.startswith("ev_")               # 缺 id 补号
    assert sp.status == "proposed"


def test_空提案兜底成一集():
    sp = StoryPlan(understanding=StoryUnderstanding(
        events=[StoryEvent(text="一件事", episode=1)])).normalized()
    assert len(sp.episodes) == 1


def test_理解卡是人话():
    card = _plan().normalized().to_card()
    assert "分集提案：共 3 集" in card
    assert "一个不删" in card
    assert "钩子：钩子1" in card


# ---------------------------------------------------------------- 任务书


def test_episode_brief_带事件底线与钩子():
    sp = _plan().normalized()
    brief = episode_brief(sp, sp.episodes[0])
    assert "β 诞生" in brief and "蛋糕初味" in brief
    assert "强制服从" not in brief               # 别集的事件不混进来
    assert "钩子1" in brief
    assert "加戏" in brief and "禁止为了压时长删事件" in brief


def test_generate_from_plan_逐集扩写并合并(monkeypatch):
    """每集单独生成，场头 episode/chapter_refs 由代码钉死，合并场号连续。"""
    import app.pipeline.generation as generation

    calls: list[dict] = []

    def fake_generate(llm, settings, **kwargs):
        calls.append(kwargs)
        return Script(
            title=kwargs["title"],
            source=Source(chapter_count=1, chapter_ids=["ch_001"]),
            logline="l",
            characters=[Character(id="char_001", name="甲")],
            locations=[Location(id="loc_001", name="屋")],
            scenes=[Scene(
                id="scene_001", title="场", chapter_refs=["ch_001"],
                location_id="loc_001", characters=["char_001"],
                purpose="p", conflict="c",
                beats=[ScriptBeat(id="beat_001", type="action", text="甲（char_001）推门。")],
            )],
        ), {}

    monkeypatch.setattr(generation, "generate_script", fake_generate)
    merged, art = generate_from_plan(
        _FakeLLM(None), None, title="替身", raw_text="原文",
        plan=_plan(status="confirmed"), adaptation_type="short_drama",
        language="zh-CN", dialogue_style="vernacular",
    )
    assert len(calls) == 3
    assert calls[0]["skip_adaptation"] is True
    assert calls[0]["dialogue_style"] == "vernacular"
    assert "本集人物范围" in calls[0]["bible_scope"]
    assert "β 诞生" in calls[0]["extra_scope"]
    assert [sc.episode for sc in merged.scenes] == [1, 2, 3]
    assert [sc.id for sc in merged.scenes] == ["scene_001", "scene_002", "scene_003"]
    assert [sc.chapter_refs for sc in merged.scenes] == [["ep_001"], ["ep_002"], ["ep_003"]]
    assert len(merged.characters) == 1 and len(merged.locations) == 1
    assert "char_" not in merged.scenes[0].beats[0].text
    assert art["episodes"] == 3 and art["scenes"] == 3
    all_ids = [b.id for sc in merged.scenes for b in sc.beats]
    assert len(all_ids) == len(set(all_ids))   # beat id 合并后必须全局唯一
    # 提案的集数/目标秒数必须随剧本交给导演（D-90）；否则导演按内容量估，
    # 421 秒/5 集的分集剧本会被拆成 6–7 集（2026-11-26 实测）。
    assert merged.adaptation_plan is not None
    assert merged.adaptation_plan.episode_count == 3
    assert merged.adaptation_plan.target_sec == 225.0
    assert set(merged.adaptation_plan.scene_targets) == {"scene_001", "scene_002", "scene_003"}


def test_adaptation_plan_for_按集目标分到各场():
    """每集 target_sec 按场内内容量分给各场，集数取实际出现的集号。"""
    script = Script(
        title="t", logline="l",
        source=Source(chapter_count=2, chapter_ids=["ep_001", "ep_002"]),
        characters=[Character(id="char_001", name="甲")],
        locations=[Location(id="loc_001", name="屋")],
        scenes=[
            _scene(1, 1, actions=1),
            _scene(2, 1, actions=3),
            _scene(3, 2, actions=2),
        ],
    )
    plan = StoryPlan(
        understanding=StoryUnderstanding(logline="l"),
        episodes=[
            EpisodeArc(index=1, title="一", target_sec=60),
            EpisodeArc(index=2, title="二", target_sec=90),
        ],
        status="confirmed",
    )
    budget = adaptation_plan_for(plan, script)
    assert budget.episode_count == 2
    assert budget.target_sec == 150.0
    # 第 1 集 60 秒按 1:3 分给两场；第 2 集 90 秒全给唯一的场
    assert round(budget.scene_targets["scene_001"], 1) == 15.0
    assert round(budget.scene_targets["scene_002"], 1) == 45.0
    assert round(budget.scene_targets["scene_003"], 1) == 90.0


def test_ensure_plan_budget_给旧版本补预算且幂等():
    """2026-11-26 之前落库的分集剧本没有 adaptation_plan：从作品提案补。"""

    class _P:
        story_plan = _plan(n_eps=3, status="confirmed").model_dump()

    script = Script(
        title="t", logline="l",
        source=Source(chapter_count=3, chapter_ids=["ep_001", "ep_002", "ep_003"]),
        characters=[Character(id="char_001", name="甲")],
        locations=[Location(id="loc_001", name="屋")],
        scenes=[_scene(1, 1), _scene(2, 2), _scene(3, 3)],
    )
    assert script.adaptation_plan is None
    assert ensure_plan_budget(_P(), script) is True
    assert script.adaptation_plan.episode_count == 3
    assert ensure_plan_budget(_P(), script) is False  # 已有预算不重复覆盖


def _scene(i: int, episode: int, *, actions: int = 1) -> Scene:
    beats = [
        ScriptBeat(id=f"beat_{i:03d}{k:02d}", type="action", text="动作。")
        for k in range(1, actions + 1)
    ]
    return Scene(
        id=f"scene_{i:03d}", title=f"场{i}", chapter_refs=[f"ep_{episode:03d}"],
        location_id="loc_001", characters=["char_001"], purpose="p", conflict="c",
        episode=episode, beats=beats,
    )


def test_generate_from_plan_跨集别名人物合并(monkeypatch):
    """逐集 Bible 把同一人物叫成「客一/客」时，合并成同一个角色；同场重复台词去重。"""
    import app.pipeline.generation as generation

    names = iter(["客一", "客", "客"])

    def fake_generate(llm, settings, **kwargs):
        name = next(names)
        line = "我明天一早就上山拜师求学。"
        return Script(
            title=kwargs["title"],
            source=Source(chapter_count=1, chapter_ids=["ch_001"]),
            logline="l",
            characters=[Character(id="char_001", name=name)],
            locations=[Location(id="loc_001", name="屋")],
            scenes=[Scene(
                id="scene_001", title="场", chapter_refs=["ch_001"],
                location_id="loc_001", characters=["char_001"],
                purpose="p", conflict="c",
                beats=[
                    ScriptBeat(id="beat_001", type="action", text="推门。"),
                    ScriptBeat(id="beat_002", type="dialogue", speaker="char_001", line=line),
                    ScriptBeat(id="beat_003", type="dialogue", speaker="char_001", line=line),
                ],
            )],
        ), {}

    monkeypatch.setattr(generation, "generate_script", fake_generate)
    merged, _ = generate_from_plan(
        _FakeLLM(None), None, title="替身", raw_text="原文",
        plan=_plan(status="confirmed"), adaptation_type="short_drama",
        language="zh-CN",
    )
    assert [c.name for c in merged.characters] == ["客一"]
    assert merged.scenes[2].characters == [merged.characters[0].id]
    # 同场逐字重复的台词只留第一句
    dup_scene = merged.scenes[2]
    assert [b.id for b in dup_scene.beats].count("beat_003") == 0
    assert len(dup_scene.dialogue) == 1


def test_generate_from_plan_说话人漏列补进场(monkeypatch):
    """台词说话人没列进本场 characters 时，代码补上（否则审计报漏人物）。"""
    import app.pipeline.generation as generation

    def fake_generate(llm, settings, **kwargs):
        return Script(
            title=kwargs["title"],
            source=Source(chapter_count=1, chapter_ids=["ch_001"]),
            logline="l",
            characters=[
                Character(id="char_001", name="甲"),
                Character(id="char_002", name="乙"),
            ],
            locations=[Location(id="loc_001", name="屋")],
            scenes=[Scene(
                id="scene_001", title="场", chapter_refs=["ch_001"],
                location_id="loc_001", characters=["char_001"],
                purpose="p", conflict="c",
                beats=[ScriptBeat(
                    id="beat_001", type="dialogue", speaker="char_002",
                    line="乙说了一句完整的话。",
                )],
            )],
        ), {}

    monkeypatch.setattr(generation, "generate_script", fake_generate)
    merged, _ = generate_from_plan(
        _FakeLLM(None), None, title="替身", raw_text="原文",
        plan=_plan(status="confirmed"), adaptation_type="short_drama",
        language="zh-CN",
    )
    assert "char_002" in merged.scenes[0].characters


# ---------------------------------------------------------------- 提案调用


class _FakeLLM:
    available = True

    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def structured(self, schema):
        outer = self

        class _R:
            def invoke(self, msgs):
                outer.calls.append(msgs)
                if isinstance(outer._resp, Exception):
                    raise outer._resp
                return outer._resp
        return _R()


def test_plan_story_成功返回归一化提案():
    llm = _FakeLLM(_plan())
    sp = plan_story(llm, title="替身", raw_text="原文" * 100)
    assert sp is not None and sp.status == "proposed"
    assert [e.index for e in sp.episodes] == [1, 2, 3]
    # 系统提示词讲清了底线
    sys = llm.calls[0][0]["content"]
    assert "一集都不会删掉任何一条" in sys
    # DeepSeek 的 json_object 模式要求提示词里出现 json，否则 400
    assert "JSON" in sys or "json" in sys


def test_plan_story_带集数提示与用户要求():
    llm = _FakeLLM(_plan(n_eps=2))
    sp = plan_story(llm, title="t", raw_text="x", episodes_hint=2, notes="结尾留白")
    assert sp is not None
    user = llm.calls[0][1]["content"]
    assert "用户已指定：做 2 集" in user
    assert "结尾留白" in user


def test_plan_story_台词语体进提示词():
    llm = _FakeLLM(_plan())
    plan_story(llm, title="t", raw_text="x", dialogue_style="vernacular")
    user = llm.calls[0][1]["content"]
    assert "白话" in user
    assert "语体转述" in user


def test_plan_story_无事件视为失败():
    empty = StoryPlan(episodes=[EpisodeArc(index=1)])
    assert plan_story(_FakeLLM(empty), title="t", raw_text="x") is None


def test_plan_story_模型炸了返回None不抛():
    assert plan_story(_FakeLLM(RuntimeError("429")), title="t", raw_text="x") is None


def test_plan_story_模型不可用返回None():
    class _No:
        available = False
    assert plan_story(_No(), title="t", raw_text="x") is None


# ---------------------------------------------------------------- 扩写接线


def test_generate_script_确认的提案注入scope_note(monkeypatch):
    """confirmed 提案 → 场景阶段带事件底线；proposed/没有 → 不带（旧行为不变）。"""
    import app.pipeline.generation as gen

    captured: dict = {}

    def fake_structured(schema):
        class _R:
            def invoke(self, msgs):
                user = msgs[-1]["content"]
                if user.startswith("请基于下面的原著片段生成剧本故事圣经"):
                    return gen.Bible(title="t", logline="l",
                                     characters=[{"name": "甲"}],
                                     locations=[{"name": "屋"}])
                captured["scenes_prompt"] = user
                return gen.ScenePlan.model_validate({"scenes": [{
                    "scene_id": "scene_001", "title": "第一场", "location": "屋",
                    "characters": ["甲"],
                    "beats": [{"type": "action", "text": "甲推门。"}],
                }]})
        return _R()

    class _LLM:
        available = True
        def system_prompt(self):
            return "sys"
        def structured(self, schema):
            return fake_structured(schema)

    from app.config import get_settings
    plan = _plan(status="confirmed")
    _script, art = gen.generate_script(
        _LLM(), get_settings(), title="t", raw_text="甲推门。",
        adaptation_type="short_drama", language="zh-CN", story_plan=plan,
    )
    assert "事件底线" in captured["scenes_prompt"]
    assert "β 诞生" in captured["scenes_prompt"]
    assert art.get("story_plan") == {"episodes": 3, "events": 5}

    # 没提案：场景提示词里没有事件底线（旧路径逐字不变）
    captured.clear()
    _s2, art2 = gen.generate_script(
        _LLM(), get_settings(), title="t", raw_text="甲推门。",
        adaptation_type="short_drama", language="zh-CN",
    )
    assert "事件底线" not in captured["scenes_prompt"]
    assert art2.get("story_plan") is None


# ---------------------------------------------------------------- 对话工具层


class _GenLLM:
    """按 schema 分派的假 LLM：StoryPlan→提案，Bible→圣经，ScenePlan→一场戏。

    工具层测试要真走完 bible/scenes 两阶段（否则 generate_script 落
    local-fallback，artifacts 里没有 story_plan，notes 断言就测不到东西）。"""

    available = True

    def system_prompt(self):
        return "sys"

    def structured(self, schema):
        def _invoke(msgs):
            from app.pipeline.generation import Bible, ScenePlan

            if schema is StoryPlan:
                return _plan()
            if schema is Bible:
                return Bible(title="替身", logline="l",
                             characters=[{"name": "甲"}], locations=[{"name": "屋"}])
            return ScenePlan.model_validate({"scenes": [{
                "scene_id": "scene_001", "title": "第一场", "location": "屋",
                "characters": ["甲"],
                "beats": [{"type": "action", "text": "甲推门。"}],
            }]})
        return type("_R", (), {"invoke": staticmethod(_invoke)})()


def _tools(store):
    from app.chat.tools import build_chat_tools

    collector = {"project_id": None, "payloads": []}
    return {t.name: t for t in build_chat_tools(store, _GenLLM(), collector)}, collector


def test_工具链_提案落库_确认后生成按集扩写(store, sample_text):
    tools, collector = _tools(store)
    tools["create_project"].invoke({"title": "替身", "raw_text": sample_text})
    pid = collector["project_id"]

    r = tools["plan_story"].invoke({"project_id": pid})
    assert "分集提案：共 3 集" in r and "待确认" in r
    p = store.get_project(pid)
    assert p.story_plan["status"] == "proposed"
    assert len(p.story_plan["understanding"]["events"]) == 5

    # 没确认就生成：旧口径（初始生成），不带扩写标签
    r = tools["generate_script"].invoke({"project_id": pid})
    assert "已生成初稿" in r
    v = store.latest_version(store.get_project(pid))
    assert "扩写" not in (v.label or "")

    # 确认后生成：版本标签按集扩写
    r = tools["confirm_story_plan"].invoke({"project_id": pid})
    assert "提案已确认" in r
    assert store.get_project(pid).story_plan["status"] == "confirmed"
    tools["generate_script"].invoke({"project_id": pid})
    v2 = store.latest_version(store.get_project(pid))
    assert v2.label == "按提案扩写（3 集）"
    assert "故事提案" in (v2.notes or "")


def test_工具层_没有提案时确认与生成都不炸(store, sample_text):
    tools, collector = _tools(store)
    tools["create_project"].invoke({"title": "替身", "raw_text": sample_text})
    pid = collector["project_id"]
    assert "还没有故事提案" in tools["confirm_story_plan"].invoke({"project_id": pid})
    # 未提案直接生成 = 旧路径
    assert "已生成初稿" in tools["generate_script"].invoke({"project_id": pid})


def test_工具层_集数参数非法要人话拒绝(store, sample_text):
    tools, collector = _tools(store)
    tools["create_project"].invoke({"title": "替身", "raw_text": sample_text})
    pid = collector["project_id"]
    r = tools["plan_story"].invoke({"project_id": pid, "episodes": "三"})
    assert "数字" in r


def test_合并时同义地点归并_场标题带集号(monkeypatch):
    """ep1 的「观宇」与 ep2 的「劳山道观」是同一座殿：描述重合度 0.414
    必须并成一个地点，否则跨集给同一座殿出两套锚；通用场号也要带集号，
    否则 17 场里会出现 4 个「第 1 场」。"""
    import app.pipeline.generation as generation

    calls = {"n": 0}

    def fake_generate(llm, settings, **kwargs):
        calls["n"] += 1
        first = calls["n"] == 1
        return Script(
            title=kwargs["title"], logline="l",
            source=Source(chapter_count=1, chapter_ids=["ch_001"]),
            characters=[Character(id="char_001", name="王生")],
            locations=[Location(
                id="loc_001",
                name="观宇" if first else "劳山道观",
                description=("位于劳山顶，甚幽；内有蒲团、案几、墙壁，道士坐蒲团上，"
                             "门人薄暮毕集" if first else
                             "劳山顶上的幽静观宇，松柏环绕，内光线幽暗，有蒲团、案几、"
                             "油灯，墙壁可粘纸月。"),
            )],
            scenes=[Scene(
                id="scene_001", title="第 1 场", chapter_refs=["ch_001"],
                location_id="loc_001", characters=["char_001"],
                purpose="p", conflict="c",
                beats=[ScriptBeat(id="beat_001", type="action", text="王生推门。")],
            )],
        ), {}

    monkeypatch.setattr(generation, "generate_script", fake_generate)
    merged, _ = generate_from_plan(
        _FakeLLM(None), None, title="劳山道士", raw_text="原文",
        plan=_plan(n_eps=2, status="confirmed"), adaptation_type="short_drama",
        language="zh-CN", dialogue_style="vernacular",
    )
    assert len(merged.locations) == 1, [l.name for l in merged.locations]
    assert [sc.title for sc in merged.scenes] == ["第1集·第1场", "第2集·第1场"]
