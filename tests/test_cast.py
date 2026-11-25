# =====================================================================
# test_cast.py —— 选角与资产库
#
# 这组测试锁的是一个真实事故：2026-10-31《孔乙己》成片里，酒客甲、酒客乙、
# 「我」三个人开口全是御姐音——因为旧的 assign_voices 用一个「男女交替」
# 列表兜底，而且列表从女声起头，第一个推断不出性别的角色就分到女声。
#
# 所以这里的断言不是「音色好不好听」，而是几条硬事实：
#   1. 没有女性线索的男角色，绝不能拿到女声；
#   2. 同片内不同角色不能共用同一把嗓子（否则观众分不清谁在说）；
#   3. 戏份最多的人先挑音色（主角不该拿群演剩下的）；
#   4. 资产库里人工指定的音色，永远压过自动推断。
# =====================================================================

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
from app.domain import Script, normalize_adaptation_type, normalize_visual_style
from app.main import create_app
from app.media.casting import (
    MALE_POOL,
    MALE_POOL_ANIME,
    cast_script,
    gender_of_voice,
    infer_gender,
)
from app.media.library import apply_library_patch, build_library
from app.video.rhythm import rhythm_table, suggest_duration


def _script(characters: list[dict], *, dialogue: dict[str, int] | None = None) -> Script:
    """造一个最小剧本：给每个角色按台词量配几句对白。"""
    dialogue = dialogue or {}
    beats = []
    n = 1
    for c in characters:
        for _ in range(dialogue.get(c["id"], 0)):
            beats.append({
                "id": f"beat_{n:03d}", "type": "dialogue",
                "speaker": c["id"], "line": "这是一句用来测音色的台词",
            })
            n += 1
    return Script(
        title="测试剧", logline="测音色分配",
        source={"chapter_count": 1, "chapter_ids": ["ch_001"]},
        characters=characters,
        locations=[{"id": "loc_001", "name": "咸亨酒店"}],
        scenes=[{
            "id": "scene_001", "title": "酒店", "location_id": "loc_001",
            "chapter_refs": ["ch_001"],
            "characters": [c["id"] for c in characters],
            "purpose": "测选角", "conflict": "谁在说话",
            "beats": beats,
        }],
    )


_KONGYIJI_CAST = [
    {"id": "char_001", "name": "酒客甲", "role": "supporting"},
    {"id": "char_002", "name": "掌柜", "role": "supporting"},
    {"id": "char_003", "name": "酒客乙", "role": "supporting"},
    {"id": "char_004", "name": "孔乙己", "role": "supporting"},
    {"id": "char_005", "name": "我", "role": "supporting"},
]


# ---------- 性别推断 ----------


@pytest.mark.parametrize("name,expected", [
    ("祥林嫂", "female"), ("张夫人", "female"), ("母亲", "female"),
    ("孔乙己", "unknown"), ("掌柜", "unknown"), ("酒客甲", "unknown"),
])
def test_强线索判性别(name, expected):
    assert infer_gender(name)[0] == expected


def test_名字用字只是弱证据():
    """「小翠」没有称谓词，靠名用字推断，必须标成推测而不是硬结论。"""
    gender, basis, source = infer_gender("小翠")
    assert gender == "female" and source == "guess" and "推测" in basis


def test_描述里的她不会把男角色判成女(fresh=True):
    """「她的父亲」这种描述会让父亲被判成女性——所以强线索只看名字。"""
    gender, _, _ = infer_gender("父亲", appearance="她的父亲站在门口")
    assert gender == "male"


# ---------- 核心：不许女声从男角色嘴里出来 ----------


def test_无性别线索的男角色不会被分到女声():
    script = _script(_KONGYIJI_CAST, dialogue={"char_004": 3})
    cast = cast_script(script)
    for m in cast.members:
        if m.name in {"酒客甲", "掌柜", "孔乙己", "我"}:
            assert gender_of_voice(m.voice_id) == "male", f"{m.name} 拿到了女声 {m.voice_id}"


def test_同片内不同角色不共用同一把嗓子():
    script = _script(_KONGYIJI_CAST, dialogue={"char_004": 5, "char_001": 3})
    cast = cast_script(script)
    voices = [m.voice_id for m in cast.members]
    # 5 个角色、4 个男声：允许溢出一次复用，但前四个必须互不相同
    assert len(set(voices[:4])) == 4


def test_戏份最多的人先挑音色():
    """主角排在第 4 个出场，也必须拿到池里第一把（磁性男声）。"""
    script = _script(_KONGYIJI_CAST, dialogue={"char_004": 9})
    cast = cast_script(script, visual_style="realistic")
    by_name = {m.name: m for m in cast.members}
    assert by_name["孔乙己"].voice_id == MALE_POOL[0]


def test_女角色一定拿女声():
    script = _script([
        {"id": "char_001", "name": "孔乙己"},
        {"id": "char_002", "name": "祥林嫂"},
    ], dialogue={"char_002": 4})
    cast = cast_script(script)
    by_name = {m.name: m for m in cast.members}
    assert by_name["祥林嫂"].gender == "female"
    assert gender_of_voice(by_name["祥林嫂"].voice_id) == "female"


def test_没有线索时会明确告警而不是静默猜():
    cast = cast_script(_script(_KONGYIJI_CAST))
    assert any("没有性别线索" in w for w in cast.warnings)


# ---------- 人工指定压过一切 ----------


def test_资产库指定的音色压过自动推断():
    script = _script(_KONGYIJI_CAST, dialogue={"char_004": 3})
    cast = cast_script(script, casting={"孔乙己": "female-shaonv"})
    m = next(x for x in cast.members if x.name == "孔乙己")
    assert m.voice_id == "female-shaonv"
    assert m.source == "casting"


def test_资产库指定的性别能纠正错误推断():
    script = _script([{"id": "char_001", "name": "掌柜"}])
    cast = cast_script(script, genders={"掌柜": "female"})
    assert gender_of_voice(cast.voices["掌柜"]) == "female"


def test_按_id_也能取到音色():
    """剧本节拍的 speaker 存的是 char_id，两种键都必须是通的。"""
    script = _script(_KONGYIJI_CAST)
    cast = cast_script(script)
    assert cast.voice_for("char_004") == cast.voices["孔乙己"]


def test_剧本里临时冒出来的人有稳定音色():
    """群演不在 characters 里：不能全挤到同一把嗓子，也不能每次变。"""
    cast = cast_script(_script(_KONGYIJI_CAST))
    a = cast.voice_for("char_999", "路人丙")
    b = cast.voice_for("char_999", "路人丙")
    assert a == b
    assert gender_of_voice(a) == "male"


# ---------- 短剧 / 漫剧 ----------


def test_短剧比电影快():
    assert suggest_duration("medium", "short_drama") < suggest_duration("medium", "film")
    assert suggest_duration("slow", "short_drama") < suggest_duration("slow", "film")


def test_漫剧是短剧的画风而不是另一个类型():
    """核心建模判断：短剧包含漫剧。

    漫剧 = 短剧 + 动画画风。所以它不该出现在 adaptation_type 里，
    只该出现在 visual_style 里；节奏表与导演逻辑完全共用，不分叉。
    """
    assert normalize_adaptation_type("漫剧") != "漫剧"          # 别名里没有它
    assert normalize_adaptation_type("漫剧") in {"other", "short_drama"}
    assert normalize_visual_style("漫剧") == "3d"
    assert normalize_visual_style("3D 漫剧") == "3d"
    assert normalize_visual_style(None) == "3d"
    assert normalize_visual_style("二次元") == "anime"
    assert normalize_visual_style("写实") == "realistic"
    # 节奏表里不存在「漫剧」这一档——它不该有独立的节奏
    assert "anime_drama" not in rhythm_table()
    assert rhythm_table("anime") == rhythm_table("other")


def test_漫剧音色池偏年轻():
    assert MALE_POOL_ANIME[0] != MALE_POOL[0]
    assert "qingse" in MALE_POOL_ANIME[0] or "daxuesheng" in MALE_POOL_ANIME[0]
    script = _script(_KONGYIJI_CAST, dialogue={"char_004": 9})
    anime = cast_script(script, visual_style="anime")
    realistic = cast_script(script, visual_style="realistic")
    assert anime.voices["孔乙己"] != realistic.voices["孔乙己"]


# ---------- 资产库 ----------


def test_资产库汇总角色与场景():
    script = _script(_KONGYIJI_CAST, dialogue={"char_004": 6, "char_001": 2})
    style = {
        "character_appearances": {"孔乙己": "穿长衫的唯一站着喝酒的人"},
        "reference_images": {"孔乙己": "https://example.com/kong.png"},
    }
    lib = build_library(script, style, visual_style="realistic")
    assert lib["characters"][0]["name"] == "孔乙己"      # 台词最多排最前
    assert lib["characters"][0]["image"].endswith("kong.png")
    assert lib["characters"][0]["speech_chars"] > 0
    assert lib["locations"][0]["name"] == "咸亨酒店"
    assert any("定妆图" in w for w in lib["warnings"])


def test_资产库修改只动指定字段():
    style = {"character_appearances": {"孔乙己": "长衫"}, "lighting_style": "冷调"}
    merged = apply_library_patch(style, {"characters": {"孔乙己": {"voice_id": "male-qn-badao"}}})
    assert merged["voice_casting"]["孔乙己"] == "male-qn-badao"
    assert merged["character_appearances"]["孔乙己"] == "长衫"   # 未提及的不动
    assert merged["lighting_style"] == "冷调"
    # 传空串 = 撤销人工指定
    cleared = apply_library_patch(merged, {"characters": {"孔乙己": {"voice_id": ""}}})
    assert "孔乙己" not in cleared["voice_casting"]


# ---------- API ----------


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    return TestClient(create_app())


def _project_with_script(client, store):
    project = store.create_project(title="孔乙己", adaptation_type="short_drama",
                                   language="zh-CN", raw_text="原文")
    script = _script(_KONGYIJI_CAST, dialogue={"char_004": 4})
    sv = store.create_version(project, script, source_type="generation", set_current=True)
    assert sv.id
    return project


def test_资产库接口能读出角色与音色(client, store):
    project = _project_with_script(client, store)
    resp = client.get(f"/api/projects/{project.id}/library")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert {c["name"] for c in data["characters"]} >= {"孔乙己", "掌柜"}
    assert data["voice_options"]
    for c in data["characters"]:
        assert c["voice_id"]


def test_改音色后立刻生效(client, store):
    project = _project_with_script(client, store)
    resp = client.patch(
        f"/api/projects/{project.id}/library",
        json={"characters": {"孔乙己": {"gender": "male", "voice_id": "male-qn-jingying"}}},
    )
    assert resp.status_code == 200, resp.text
    kong = next(c for c in resp.json()["characters"] if c["name"] == "孔乙己")
    assert kong["voice_id"] == "male-qn-jingying"
    assert kong["confirmed"] is True
    # 再读一次仍然在（已落库）
    again = client.get(f"/api/projects/{project.id}/library").json()
    assert next(c for c in again["characters"] if c["name"] == "孔乙己")["voice_id"] == "male-qn-jingying"
