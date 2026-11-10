# =====================================================================
# test_voice_pool.py —— BYOK 音色发现 → 勾选 → 下游生效 的整条路
#
# 锁四件事（都是「拿用户的 key 问厂商有什么，用户只管勾选」这个诉求的落点）：
#   1. 发现的音色落库、勾选后 enabled 池变化；重新发现不冲掉已勾选；
#   2. 自动选角优先用勾选的音色；other（厂商不给性别）不参与自动、只供手动指定；
#   3. 没勾选过时一切与改造前一致（内置 8 个兜底，测试契约不破坏）；
#   4. /voices/discover 端点：假 TTS 客户端走通 发现→落库→勾选→下拉 的往返。
# =====================================================================

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
from app.agent.tasks import SubAgentRunner
from app.main import create_app
from app.media.casting import VOICE_LABELS, build_voice_pool, cast_script
from app.domain import Character, Location, Scene, Script, ScriptBeat, Source


def _client(store, settings, monkeypatch) -> TestClient:
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: None)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    monkeypatch.setattr(
        api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store)
    )
    return TestClient(create_app())


def _script() -> Script:
    return Script(
        title="t", logline="l", themes=[],
        source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[
            Character(id="char_001", name="主角", role="protagonist", gender="male"),
            Character(id="char_002", name="女主", role="supporting", gender="female"),
        ],
        locations=[Location(id="loc_001", name="街")],
        scenes=[Scene(
            id="scene_001", title="s", chapter_refs=["ch_001"], location_id="loc_001",
            characters=["char_001", "char_002"], purpose="p", conflict="c",
            beats=[
                ScriptBeat(id="beat_001", type="dialogue", speaker="char_001", line="一"),
                ScriptBeat(id="beat_002", type="dialogue", speaker="char_002", line="二"),
            ],
        )],
    )


# ---------- 1) store 层：落库 / 勾选 / 重新发现不冲掉 ----------


def test_discover_upsert_then_enable(store):
    store.upsert_voice_pool([
        {"voice_id": "male-a", "name": "A 男声", "gender": "male"},
        {"voice_id": "female-b", "name": "B 女声", "gender": "female"},
        {"voice_id": "clever_boy", "name": "聪明男童", "gender": "other"},
    ])
    assert len(store.list_voice_pool()) == 3
    assert store.list_voice_pool(enabled_only=True) == []  # 发现≠启用

    n = store.set_voice_pool_enabled(["male-a", "clever_boy"])
    assert n == 2
    assert sorted(v.voice_id for v in store.list_voice_pool(enabled_only=True)) == ["clever_boy", "male-a"]

    # 重新发现：新行进来，已勾选的不被冲掉
    store.upsert_voice_pool([{"voice_id": "female-c", "name": "C 女声", "gender": "female"}])
    assert sorted(v.voice_id for v in store.list_voice_pool(enabled_only=True)) == ["clever_boy", "male-a"]


# ---------- 2) 选角层：勾选优先、other 不自动、空池回退 ----------


def test_enabled_voices_take_priority_in_casting(store):
    store.upsert_voice_pool([
        {"voice_id": "male-x", "name": "X 男声", "gender": "male"},
        {"voice_id": "female-y", "name": "Y 女声", "gender": "female"},
    ])
    store.set_voice_pool_enabled(["male-x", "female-y"])
    pool = build_voice_pool(store, "realistic")
    cast = cast_script(_script(), voice_pool=pool)
    by = {m.name: m for m in cast.members}
    assert by["主角"].voice_id == "male-x"      # 勾的排最前，自动选角先用
    assert by["女主"].voice_id == "female-y"
    assert by["主角"].voice_label == "X 男声"    # 显示厂商名，不是原始 id（D-24）


def test_other_gender_never_auto_cast(store):
    """other（厂商不给性别）只进下拉供手动指定，绝不参与自动选角。

    猜性别出过「男主开口是女声」的事故（见 docs 5.19），这条是红线。
    """
    store.upsert_voice_pool([{"voice_id": "clever_boy", "name": "聪明男童", "gender": "other"}])
    store.set_voice_pool_enabled(["clever_boy"])
    pool = build_voice_pool(store, "realistic")
    cast = cast_script(_script(), voice_pool=pool)
    # 男声池里只有内置兜底（勾选的 clever_boy 是 other，被剔除）
    assert all(m.voice_id != "clever_boy" for m in cast.members)
    assert cast.members[0].voice_id in VOICE_LABELS  # 回退内置


def test_empty_pool_identical_to_legacy(store):
    """没勾选过：池=内置 8 个按画风排，与改造前行为完全一致。"""
    pool = build_voice_pool(store, "anime")
    assert [p["voice_id"] for p in pool if p["gender"] == "male"][:1] == ["male-qn-qingse"]  # 漫剧年轻在前
    # 同画风下：注入空勾选池 vs 完全不注入，选角结果必须逐位一致
    cast = cast_script(_script(), visual_style="anime", voice_pool=pool)
    legacy = cast_script(_script(), visual_style="anime")
    assert [m.voice_id for m in cast.members] == [m.voice_id for m in legacy.members]


def test_clone_voices_are_other_and_persisted(store):
    """克隆音色进池（other），且批量勾选不把它误关。"""
    store.save_user_voice(voice_id="wenju_clone_1", name="孔乙己的声音", project_id="p1")
    store.upsert_voice_pool([{"voice_id": "male-a", "name": "A", "gender": "male"}])
    store.set_voice_pool_enabled([])
    pool = build_voice_pool(store, "realistic")
    clone = next(p for p in pool if p["voice_id"] == "wenju_clone_1")
    assert clone["gender"] == "other" and clone["name"] == "孔乙己的声音"


# ---------- 3) 端点往返：discover → pool → 下拉 ----------


class _FakeTTS:
    label = "MiniMax 语音合成"

    def list_system_voices(self, *, timeout: float = 30.0):
        return [
            {"voice_id": "male-qn-badao", "name": "霸道青年音色", "gender": "male"},
            {"voice_id": "badao_shaoye", "name": "霸道少爷", "gender": "other"},
        ]


def test_discover_and_enable_endpoints(store, settings, monkeypatch):
    import app.llm.tts as tts_mod

    monkeypatch.setattr(tts_mod, "build_tts_client", lambda cfg: _FakeTTS())
    client = _client(store, settings, monkeypatch)

    r = client.post("/api/voices/discover")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2

    r = client.get("/api/voices/pool")
    body = r.json()
    assert len(body["all"]) == 2 and body["enabled"] == []

    r = client.put("/api/voices/pool", json={"voice_ids": ["badao_shaoye"]})
    assert r.status_code == 200 and r.json()["enabled"] == 1

    r = client.get("/api/voices/pool")
    assert [v["voice_id"] for v in r.json()["enabled"]] == ["badao_shaoye"]


def test_discover_without_key_reports_honestly(store, settings, monkeypatch):
    """没配 key：如实 503，不假装发现成功（编造护栏同款原则）。"""
    import app.llm.tts as tts_mod

    monkeypatch.setattr(tts_mod, "build_tts_client", lambda cfg: None)
    client = _client(store, settings, monkeypatch)
    assert client.post("/api/voices/discover").status_code == 503


def test_library_dropdown_reflects_enabled(store, settings, monkeypatch, sample_text):
    """勾选后，资产库下拉 = 勾选在前 + 内置兜底，角色选择器立刻变丰富。"""
    client = _client(store, settings, monkeypatch)
    pid = client.post(
        "/api/projects/import", data={"raw_text": sample_text, "title": "音色往返"},
    ).json()["id"]

    store.upsert_voice_pool([{"voice_id": "junlang_nanyou", "name": "俊朗男友", "gender": "male"}])
    store.set_voice_pool_enabled(["junlang_nanyou"])

    # /library/voices 不依赖剧本版本（新作品也能先挑音色）
    opts = client.get(f"/api/projects/{pid}/library/voices").json()
    ids = [o["voice_id"] for o in opts]
    assert "junlang_nanyou" in ids                      # 勾选的进下拉
    assert len(opts) >= 9                                # 内置 8 个还在
    assert len(ids) == len(set(ids))                     # 去重：内置的不重复出现
    # 自动选角优先用勾选的：build_voice_pool 把 enabled 排最前
    pool = build_voice_pool(store, "realistic")
    assert pool[0]["voice_id"] == "junlang_nanyou"
