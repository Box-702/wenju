# =====================================================================
# test_repair.py —— 台词一致性修复（2026-11-20，C2）
#
# 对标即梦「生成视频台词和剧本对不上 → 定位 → 按原文替换 → 只重出受影响
# 片段」的能力。确定性、零 LLM：分叉从剧本节拍与提示词句式比对里来，
# 修复只动台词句，改过的单元打回待审。
# =====================================================================

from __future__ import annotations

from app.domain import (
    Character,
    Location,
    Scene,
    Script,
    ScriptBeat,
    Source,
)
from app.pipeline.repair import (
    extract_prompt_dialogue,
    find_divergences,
    prompt_has_english,
    repair_dialogue,
    replace_text,
)


def _script(lines: list[tuple[str, str]]) -> Script:
    """一条一场的剧本：beats 里只有对白（台词逐字即权威）。"""
    beats = [ScriptBeat(id=f"beat_{i:03d}", type="dialogue", speaker="char_001", line=t)
             for i, (_, t) in enumerate(lines, 1)]
    refs = [b.id for b in beats]
    scenes = []
    for i, (_prompt_line, _) in enumerate(lines, 1):
        scenes.append(Scene(
            id=f"scene_{i:03d}", title=f"第{i}场", chapter_refs=["ch_001"],
            location_id="loc_001", characters=["char_001"], purpose="p", conflict="c",
            beats=[beats[i - 1]],
        ))
    return Script(
        title="t", version="1.0", language="zh-CN", logline="l",
        source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[Character(id="char_001", name="林远")],
        locations=[Location(id="loc_001", name="书房")],
        scenes=scenes,
    )


def _shot(sid: str, scene: str, prompt: str, order: int, **kw) -> dict:
    d = {"id": sid, "scene_id": scene, "order": order,
         "video_prompt": prompt, "prompt_status": "approved",
         "duration_sec": 5.0}
    d.update(kw)
    return d


# ---------------------------------------------------------------- 提取


def test_提取官方句式台词():
    p = "夜晚书房。林远说话：「别紧张。」然后转身。掌柜写下「十九文」"
    got = extract_prompt_dialogue(p)
    assert got == [("林远", "别紧张。")]      # 招牌引用（无冒号前动词）不算


def test_英文提示词按英文句式():
    p = 'A quiet room. He whispers: "I knew it." Camera holds.'
    assert prompt_has_english(p)
    assert extract_prompt_dialogue(p) == [("He", "I knew it.")]


# ---------------------------------------------------------------- 分叉定位


def test_台词被意译判wrong():
    script = _script([("a", "不出一月，定然还清。")])
    shots = [_shot("shot_scene_001_000", "scene_001",
                   "掌柜的手提笔。掌柜说话：「一个月肯定还你。」", 0,
                   dialogue_ref="beat_001")]
    rep = find_divergences(script, shots)
    assert len(rep.divergences) == 1
    d = rep.divergences[0]
    assert d.kind == "wrong" and d.expected == "不出一月，定然还清。"
    assert "一个月肯定还你" in d.found


def test_台词整段漏写判missing():
    script = _script([("a", "你终于来了。")])
    shots = [_shot("shot_scene_001_000", "scene_001",
                   "他站在门口，雨水顺着衣角滴落。", 0,
                   dialogue_ref="beat_001")]
    rep = find_divergences(script, shots)
    assert rep.divergences and rep.divergences[0].kind == "missing"


def test_台词逐字命中不误报():
    script = _script([("a", "你终于来了。")])
    shots = [_shot("shot_scene_001_000", "scene_001",
                   "他抬起头。林远说话：「你终于来了。」", 0,
                   dialogue_ref="beat_001")]
    assert not find_divergences(script, shots).divergences


def test_组头查整组_组员台词都在组提示词里才算齐():
    script = _script([("a", "第一句。"), ("b", "第二句。")])
    # 把两句对白放进两场（组跨场不成组，这里用同场两机位模拟）
    script.scenes[1].id = "scene_001"
    shots = [
        _shot("shot_scene_001_000", "scene_001",
              "镜头1（4秒）：甲抬头。林远说话：「第一句。」", 0,
              dialogue_ref="beat_001", group_id="g1", group_index=0,
              group_prompt="镜头1（4秒）：甲抬头。林远说话：「第一句。」"),
        _shot("shot_scene_001_001", "scene_001",
              "乙点头。", 1, dialogue_ref="beat_002",
              group_id="g1", group_index=1),
    ]
    rep = find_divergences(script, shots)
    assert rep.checked == 1                      # 组只查一次（组头）
    assert any(d.kind == "missing" and "第二句" in d.expected for d in rep.divergences)


# ---------------------------------------------------------------- 修复


def test_修复把意译改回原文并打回待审():
    script = _script([("a", "不出一月，定然还清。")])
    shots = [_shot("shot_scene_001_000", "scene_001",
                   "掌柜的手提笔。掌柜说话：「一个月肯定还你。」", 0,
                   dialogue_ref="beat_001")]
    rep = repair_dialogue(script, shots)
    assert rep.repaired_shots == ["shot_scene_001_000"]
    assert "不出一月，定然还清。" in shots[0]["video_prompt"]
    assert "一个月肯定还你" not in shots[0]["video_prompt"]
    assert shots[0]["prompt_status"] == "needs_review"
    # 修复后再查：干净
    assert not find_divergences(script, shots).divergences


def test_修复漏写把台词插进动作链中段():
    script = _script([("a", "辛苦了，喝点水吧。")])
    shots = [_shot("shot_scene_001_000", "scene_001",
                   "苏晴端着水杯走来。她把杯子放在茶几上。", 0,
                   dialogue_ref="beat_001")]
    rep = repair_dialogue(script, shots)
    prompt = shots[0]["video_prompt"]
    assert "林远说话：「辛苦了，喝点水吧。」" in prompt   # 说话人以剧本为权威
    assert prompt.index("辛苦了") < prompt.index("她把杯子放在茶几上")
    assert rep.repaired_shots == ["shot_scene_001_000"]


def test_无分叉时修复不动任何数据():
    script = _script([("a", "你来了。")])
    shots = [_shot("shot_scene_001_000", "scene_001",
                   "他抬头。林远说话：「你来了。」", 0, dialogue_ref="beat_001")]
    rep = repair_dialogue(script, shots)
    assert rep.repaired_shots == []
    assert shots[0]["prompt_status"] == "approved"


# ---------------------------------------------------------------- 精确替换


def test_替换唯一匹配才动():
    shots = [
        _shot("s1", "scene_001", "他说话：「旧词。」转身。", 0),
        _shot("s2", "scene_001", "旧词出现在这里但不是台词。", 1),
    ]
    # old 带句号：s1 命中一次（改），s2 没有「旧词。」这个串（跳过）
    changed, rejected = replace_text(shots, "旧词。", "新词。")
    assert changed == ["s1"] and not rejected
    assert "新词" in shots[0]["video_prompt"]
    assert "旧词出现在这里" in shots[1]["video_prompt"]   # s2 原样


def test_同镜头多处匹配无上下文则拒绝():
    shots = [_shot("s1", "scene_001", "甲说「来了」。乙说「来了」。", 0)]
    changed, rejected = replace_text(shots, "来了", "走了")
    assert changed == []
    assert rejected and "上下文" in rejected[0]


def test_带上下文定位只动那一处():
    shots = [_shot("s1", "scene_001", "甲说「来了」。乙说「来了」。", 0)]
    changed, rejected = replace_text(shots, "来了", "走了", context="乙说")
    assert changed == ["s1"] and not rejected
    p = shots[0]["video_prompt"]
    assert p.count("来了") == 1 and p.count("走了") == 1
    assert p.index("走了") > p.index("来了")      # 动的是乙那句
    assert shots[0]["prompt_status"] == "needs_review"


# ---------------------------------------------------------------- 对话工具层


def _seed_project(store, llm_mod):
    """造一个「剧本+提示词有台词分叉」的作品，返回 (tools, pid)。"""
    from app.chat.tools import build_chat_tools

    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm_mod, collector)}
    tools["create_project"].invoke({"title": "替身", "raw_text": "林远：不出一月，定然还清。"})
    pid = collector["project_id"]
    return tools, pid


def test_工具_检查台词一致性_报分叉并给出路(store):
    from app.pipeline.generation import Bible, ScenePlan

    class _LLM:
        available = True
        def system_prompt(self): return "s"
        def structured(self, schema):
            def _inv(msgs):
                if schema is Bible:
                    return Bible(title="t", logline="l",
                                 characters=[{"name": "林远", "id": "char_001"}],
                                 locations=[{"name": "书房"}])
                return ScenePlan.model_validate({"scenes": [{
                    "scene_id": "scene_001", "title": "第一场", "location": "书房",
                    "characters": ["林远"],
                    "beats": [{"type": "dialogue", "speaker": "char_001",
                              "line": "不出一月，定然还清。", "id": "beat_001"}],
                }]})
            return type("_R", (), {"invoke": staticmethod(_inv)})()

    tools, pid = _seed_project(store, _LLM())
    tools["generate_script"].invoke({"project_id": pid})
    # 手工造一版「台词被改写」的提示词版本
    p = store.get_project(pid)
    v = store.create_video_version(
        project_id=p.id,
        shots=[_shot("shot_scene_001_000", "scene_001",
                     "掌柜的手提笔。林远说话：「一个月肯定还你。」", 0,
                     dialogue_ref="beat_001")],
        label="台词分叉样本",
    )
    r = tools["find_prompt_divergence"].invoke({"project_id": pid})
    assert "1 处台词分叉" in r and "改写" in r and "要我按剧本原文修复吗" in r

    r2 = tools["repair_prompt_dialogue"].invoke({"project_id": pid})
    assert "已修复 1 个提交单元" in r2 and "改写 1 处" in r2
    latest = sorted(store.list_video_versions(p.id), key=lambda x: x.created_at)[-1]
    assert latest.label == "台词一致性修复"
    shot = next(s for s in latest.shots if s["id"] == "shot_scene_001_000")
    assert "不出一月，定然还清。" in shot["video_prompt"]
    assert shot["prompt_status"] == "needs_review"
    # 修完再查：干净
    assert "检查通过" in tools["find_prompt_divergence"].invoke({"project_id": pid})


def test_工具_替换多处匹配要上下文(store):
    class _NoLLM:
        available = False
    tools, pid = _seed_project(store, _NoLLM())
    p = store.get_project(pid)
    store.create_video_version(
        project_id=p.id,
        shots=[_shot("s1", "scene_001", "甲说「来了」。乙说「来了」。", 0)],
        label="样本",
    )
    r = tools["replace_in_prompts"].invoke(
        {"project_id": pid, "old_text": "来了", "new_text": "走了"})
    assert "上下文" in r
    r2 = tools["replace_in_prompts"].invoke(
        {"project_id": pid, "old_text": "来了", "new_text": "走了", "context": "乙说"})
    assert "已在 1 个镜头" in r2
