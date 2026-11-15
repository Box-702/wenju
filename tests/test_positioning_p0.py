# =====================================================================
# test_positioning_p0.py —— POSITIONING P0 三项（集-镜 / 演员库 / 提示词包）
#
#   A. 集-镜两级结构 + 生产进度百分比（D-63）
#   B. 跨项目演员库（cast_profiles 表，D-64）
#   C. 提示词包导出（md / json / 剪映草稿，D-65）
# =====================================================================

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
from app.agent.tasks import SubAgentRunner
from app.main import create_app


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    monkeypatch.setattr(
        api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store)
    )
    return TestClient(create_app())


def _script(chapters=("ch_001", "ch_002")):
    from app.domain import Script

    scenes = []
    for i, ch in enumerate(chapters, start=1):
        scenes.append({
            "id": f"scene_{i:03d}", "title": f"第{i}场", "chapter_refs": [ch],
            "location_id": "loc_01", "characters": ["char_ke"],
            "purpose": "p", "conflict": "c",
        })
    return Script(
        title="排钱", logline="排钱", themes=["体面"],
        characters=[{"id": "char_ke", "name": "孔乙己"}],
        locations=[{"id": "loc_01", "name": "咸亨酒店"}],
        scenes=scenes,
        source={"chapter_count": len(chapters), "chapter_ids": list(chapters)},
    )


def _project(store, title="排钱", chapters=("ch_001", "ch_002")):
    project = store.create_project(
        title=title, adaptation_type="short_drama", language="zh-CN", raw_text="原文",
    )
    store.create_version(project, _script(chapters), source_type="generation", label="初稿")
    return project


# ---------- A. 集-镜两级 ----------


def test_分集_集就是小说的一章(store):
    """集 = 章：场景的 chapter_refs 决定它属于第几集（D-63）。"""
    from app.crew.director import assign_episodes

    project = _project(store)
    version = store.latest_version(project)
    from app.domain import SceneBreakdown, Shot

    breakdowns = [
        SceneBreakdown(
            scene_id="scene_001",
            shots=[Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                        shot_type="medium", duration_sec=4)],
        ),
        SceneBreakdown(
            scene_id="scene_002",
            shots=[Shot(id="shot_scene_002_000", scene_id="scene_002", order=1,
                        shot_type="medium", duration_sec=4)],
        ),
    ]
    assign_episodes(breakdowns, version.script)
    assert breakdowns[0].shots[0].episode_id == "ch_001"
    assert breakdowns[0].shots[0].episode_index == 0
    assert breakdowns[1].shots[0].episode_id == "ch_002"
    assert breakdowns[1].shots[0].episode_index == 1


def test_分集_单章短剧全片一集(store):
    from app.crew.director import assign_episodes
    from app.domain import SceneBreakdown, Shot

    project = _project(store, chapters=("ch_001",))
    version = store.latest_version(project)
    breakdowns = [
        SceneBreakdown(
            scene_id="scene_001",
            shots=[Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                        shot_type="medium", duration_sec=4)],
        ),
    ]
    assign_episodes(breakdowns, version.script)
    assert breakdowns[0].shots[0].episode_id == "ch_001"
    assert breakdowns[0].shots[0].episode_index == 0


def test_概览_给集数与生产进度(client, store):
    project = _project(store)
    store.create_video_version(
        project_id=project.id,
        shots=[
            {"id": "shot_a_000", "scene_id": "scene_001", "order": 0, "duration_sec": 4,
             "episode_id": "ch_001", "episode_index": 0, "prompt_status": "approved"},
            {"id": "shot_a_001", "scene_id": "scene_002", "order": 1, "duration_sec": 3,
             "episode_id": "ch_002", "episode_index": 1, "prompt_status": "needs_review"},
        ],
        source_type="agent",
    )
    body = client.get(f"/api/projects/{project.id}/overview").json()
    assert body["episodes"] == 2
    assert body["shots"] == 2
    assert 0 < body["progress_pct"] < 100, "一半批准 → 进度在一半与满之间"
    stages = {s["key"]: s["done"] for s in body["progress_stages"]}
    assert stages["script"] is True
    assert stages["breakdown"] is True
    assert stages["prompts"] is False


# ---------- B. 演员库 ----------


def test_演员库_增删查(client, store):
    r = client.post("/api/cast-profiles", json={
        "name": "孔乙己", "gender": "male",
        "appearance": "短发，灰色风衣，左腿微跛",
        "voice_id": "male-qn-badao",
    })
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert r.json()["appearance"].startswith("短发")

    listed = client.get("/api/cast-profiles").json()
    assert any(p["id"] == pid for p in listed)

    assert client.delete(f"/api/cast-profiles/{pid}").status_code == 200
    assert client.delete(f"/api/cast-profiles/{pid}").status_code == 404


def test_演员库_应用到作品角色(client, store):
    project = _project(store)
    asset = store.add_asset(
        project.id, kind="image", name="孔乙己定妆", source="upload",
        url="/api/assets/file/img1", content=b"png",
    )
    profile = client.post("/api/cast-profiles", json={
        "name": "孔乙己", "gender": "male",
        "appearance": "短发，灰色风衣",
        "voice_id": "male-qn-badao",
        "image_asset_id": asset.id,
    }).json()

    r = client.post(f"/api/projects/{project.id}/library/apply-profile", json={
        "profile_id": profile["id"], "entity": "孔乙己",
    })
    assert r.status_code == 200, r.text
    style = store.get_video_style(project.id)
    assert style["character_appearances"].get("char_ke") == "短发，灰色风衣"
    assert style["reference_images"].get("char_ke") == "/api/assets/file/img1"
    assert style["voice_casting"].get("char_ke") == "male-qn-badao"
    assert style["character_genders"].get("char_ke") == "male"


def test_演员库_角色名对不上要拒(client, store):
    project = _project(store)
    profile = client.post("/api/cast-profiles", json={
        "name": "孔乙己", "appearance": "短发",
    }).json()
    r = client.post(f"/api/projects/{project.id}/library/apply-profile", json={
        "profile_id": profile["id"], "entity": "查无此人",
    })
    assert r.status_code == 404, r.text


# ---------- C. 提示词包 ----------


def _version_with_shots(store, project_id):
    return store.create_video_version(
        project_id=project_id,
        shots=[
            {
                "id": "shot_a_000", "scene_id": "scene_001", "order": 0,
                "duration_sec": 4, "episode_id": "ch_001", "episode_index": 0,
                "shot_type": "medium", "subject": "客人", "action": "排钱",
                "camera": {"type": "static", "speed": "medium", "height": "eye"},
                "spatial": "柜台外侧，距镜头 2.4m", "lighting": "冷调侧逆光",
                "final_frame": "铜钱摊在柜台上", "cut_reason": "动作单元切换",
                "video_prompt": "环境锚。人物锚。排钱。", "prompt_status": "approved",
            },
            {
                "id": "shot_a_001", "scene_id": "scene_002", "order": 1,
                "duration_sec": 3, "episode_id": "ch_002", "episode_index": 1,
                "shot_type": "close_up", "subject": "掌柜", "action": "打量",
                "camera": {"type": "pan", "direction": "右", "speed": "slow"},
                "video_prompt": "环境锚。人物锚。打量。", "prompt_status": "needs_review",
            },
        ],
        source_type="agent",
    )


def test_提示词包_md(client, store):
    project = _project(store)
    store.merge_video_style(project.id, {
        "visual_style": "anime",
        "character_appearances": {"char_ke": "短发，灰色风衣"},
        "environment_descriptions": {"loc_01": "昏黄路灯下的当铺"},
    })
    version = _version_with_shots(store, project.id)

    r = client.get(f"/api/video-versions/{version.id}/export-prompt-pack?fmt=md")
    assert r.status_code == 200, r.text
    text = r.content.decode("utf-8")
    assert "# 提示词包" in text
    assert "第 1 集" in text and "第 2 集" in text
    assert "一致性约束" in text
    assert "短发，灰色风衣" in text
    assert "首帧图提示词" in text
    assert "排钱。" in text


def test_提示词包_json(client, store):
    import json as _json

    project = _project(store)
    version = _version_with_shots(store, project.id)
    r = client.get(f"/api/video-versions/{version.id}/export-prompt-pack?fmt=json")
    assert r.status_code == 200, r.text
    data = _json.loads(r.content.decode("utf-8"))
    assert data["shot_count"] == 2
    assert data["total_seconds"] == 7
    assert len(data["episodes"]) == 2
    assert data["episodes"][0]["shots"][0]["shot_id"] == "shot_a_000"
    assert data["episodes"][0]["shots"][0]["shot_type_zh"] == "中景"


def test_提示词包_剪映草稿(client, store):
    import json as _json

    project = _project(store)
    version = _version_with_shots(store, project.id)
    r = client.get(f"/api/video-versions/{version.id}/export-prompt-pack?fmt=jianying")
    assert r.status_code == 200, r.text
    data = _json.loads(r.content.decode("utf-8"))
    assert len(data["materials"]) == 2
    seg = data["tracks"][0]["segments"]
    assert seg[0]["target_timerange"]["start"] == 0.0
    assert seg[1]["target_timerange"]["start"] == 4.0
    assert data["duration"] == 7.0


def test_提示词包_格式不对要拒(client, store):
    project = _project(store)
    version = _version_with_shots(store, project.id)
    r = client.get(f"/api/video-versions/{version.id}/export-prompt-pack?fmt=docx")
    assert r.status_code == 400, r.text
