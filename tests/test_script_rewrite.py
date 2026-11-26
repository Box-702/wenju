# =====================================================================
# test_script_rewrite.py —— 白话改写后处理 + 卡点补齐（2026-11-26）
# =====================================================================

from __future__ import annotations

from types import SimpleNamespace

from app.domain import Character, Location, Scene, Script, ScriptBeat, Source
from app.pipeline.script_rewrite import apply_rewrite_items, collect_rewrite_items
from app.pipeline.script_text import audit_script_text, ensure_hook_card


def _script() -> Script:
    return Script(
        title="t", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        logline="王生求穿墙术。",
        characters=[
            Character(id="char_001", name="王生", role="protagonist",
                      description="邑人王生，行七，故家子也。少慕道，聞勞山多仙人，遂負笈往遊焉。"),
            Character(id="char_002", name="道士", role="supporting",
                      description="白发垂领，笑曰「汝之惰也，吾早言之矣」"),
        ],
        locations=[Location(id="loc_001", name="观宇")],
        scenes=[Scene(
            id="scene_001", title="拜师", chapter_refs=["ch_001"],
            location_id="loc_001", time="日", interior_exterior="内景",
            characters=["char_001", "char_002"], purpose="拜师", conflict="怕吃苦",
            beats=[
                ScriptBeat(id="beat_001", type="action", text="王生走到道士面前"),
                ScriptBeat(id="beat_002", type="dialogue", speaker="char_001",
                           line="弟子诚心求道，能之。"),
                ScriptBeat(id="beat_003", type="dialogue", speaker="char_002",
                           line="汝之惰也，吾早言之矣。"),
                ScriptBeat(id="beat_004", type="os", speaker="char_001",
                           line="此术若成，吾何惧墙乎？"),
            ],
        )],
        dialogue_style="vernacular",
    )


def test_收集需要改白话的条目() -> None:
    items = collect_rewrite_items(_script())
    keys = {it["key"] for it in items}
    assert "char:char_001:description" in keys
    assert "char:char_002:description" in keys
    assert "beat:scene_001:beat_003" in keys
    assert "beat:scene_001:beat_004" in keys


def test_应用改写并降低文言() -> None:
    s = _script()
    items = [
        {"key": "char:char_001:description", "text": "城里王家的七公子，从小想学仙术。"},
        {"key": "char:char_002:description", "text": "白发老道士，笑着说：我早说你懒。"},
        {"key": "beat:scene_001:beat_003", "text": "我早说你吃不了这份苦。"},
        {"key": "beat:scene_001:beat_004", "text": "要是学会了穿墙，我还怕什么墙？"},
    ]
    changed = apply_rewrite_items(s, items)
    assert changed == 4
    assert "邑人" not in s.characters[0].description
    assert s.scenes[0].beats[2].line == "我早说你吃不了这份苦。"
    assert not any("文言" in i for i in audit_script_text(s))


def test_改写太短或仍文言时不覆盖() -> None:
    s = _script()
    old = s.scenes[0].beats[2].line
    apply_rewrite_items(s, [{"key": "beat:scene_001:beat_003", "text": "汝惰。"}])
    assert s.scenes[0].beats[2].line == old


def test_唱词加引号保留原文也算收口() -> None:
    s = _script()
    apply_rewrite_items(s, [{
        "key": "beat:scene_001:beat_003",
        "text": "「汝之惰也，吾早言之矣。」",
    }])
    assert s.scenes[0].beats[2].line == "「汝之惰也，吾早言之矣。」"
    assert not any("beat_003" in i and "文言" in i for i in audit_script_text(s))


def test_没有卡点时补开场卡点_不重复() -> None:
    s = _script()
    added = ensure_hook_card(s, SimpleNamespace(hook="王生额头鼓起大包"))
    assert added == 1
    assert s.scenes[0].beats[0].type == "card"
    assert "王生额头鼓起大包" in s.scenes[0].beats[0].text
    assert ensure_hook_card(s, SimpleNamespace(hook="再来一次")) == 0
