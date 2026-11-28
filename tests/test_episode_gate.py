# =====================================================================
# test_episode_gate.py —— 按集推进的硬闸门（D-99）
# =====================================================================
from __future__ import annotations

from app.domain import (
    Character, Location, Scene, Script, ScriptBeat, Shot, Source,
)
from app.video.episode_gate import (
    active_episode_status, advance_episode, episode_statuses, switch_episode,
)


def _script() -> Script:
    return Script(
        title="t", version="1.0", language="zh-CN", logline="l",
        source=Source(chapter_count=2, chapter_ids=["ep_001", "ep_002"]),
        characters=[Character(id="char_001", name="甲")],
        locations=[Location(id="loc_001", name="屋")],
        scenes=[
            Scene(id="scene_001", title="第1集·第1场", chapter_refs=["ep_001"],
                  location_id="loc_001", characters=["char_001"], purpose="p",
                  conflict="c", episode=1,
                  beats=[ScriptBeat(id="beat_001", type="action", text="甲起身。")]),
            Scene(id="scene_002", title="第2集·第1场", chapter_refs=["ep_002"],
                  location_id="loc_001", characters=["char_001"], purpose="p",
                  conflict="c", episode=2,
                  beats=[ScriptBeat(id="beat_002", type="action", text="甲出门。")]),
        ],
    )


def _shot(i: int) -> Shot:
    return Shot(
        id=f"shot_scene_001_{i:03d}", scene_id="scene_001", order=i,
        duration_sec=4.0, subject="甲", action="甲起身。",
        video_prompt="甲起身。",
    )


def test_按集闸门_当前集完成才能进下一集(store):
    p = store.create_project(title="t", adaptation_type="short_drama",
                             language="zh-CN", raw_text="原文")
    store.create_version(p, _script(), source_type="generation", label="t", set_current=True)
    store.set_active_episode(p.id, 1)

    status = active_episode_status(store, p)
    assert status["active"] == 1 and status["episodes"] == [1, 2]
    assert not status["complete"]
    ok, msg, _ = advance_episode(store, p)
    assert not ok and "还没有分镜" in msg

    shots = [{**_shot(0).model_dump(), "prompt_status": "approved"}]
    v = store.create_video_version(project_id=p.id, shots=shots,
                                   episode=1, label="第1集")
    status = active_episode_status(store, p)
    assert status["shots"] == 1 and status["approved"]
    assert not status["complete"]  # 还没出片

    j = store.create_video_job(project_id=p.id, shot_id=_shot(0).id,
                               provider="minimax", model="m", prompt="甲起身。",
                               version_id=v.id)
    store.update_video_job(j.id, status="succeeded", video_url="https://x/a.mp4")
    status = active_episode_status(store, p)
    assert status["complete"], status

    ok, msg, status = advance_episode(store, p)
    assert ok and status["active"] == 2

    ok, _, _ = switch_episode(store, p, 3)
    assert not ok  # 不能跳集
    ok, _, status = switch_episode(store, p, 1)
    assert ok and status["active"] == 1


def test_按集版本_不串集(store):
    p = store.create_project(title="t", adaptation_type="short_drama",
                             language="zh-CN", raw_text="原文")
    v1 = store.create_video_version(project_id=p.id, shots=[], episode=1, label="1")
    v2 = store.create_video_version(project_id=p.id, shots=[], episode=2, label="2")
    assert [v.id for v in store.list_video_versions(p.id, episode=1)] == [v1.id]
    assert store.latest_video_version(p, episode=2).id == v2.id
    assert store.latest_video_version(p, episode=3) is None


def test_分集状态列表_每集独立(store):
    p = store.create_project(title="t", adaptation_type="short_drama",
                             language="zh-CN", raw_text="原文")
    store.create_version(p, _script(), source_type="generation", label="t", set_current=True)
    v = store.create_video_version(
        project_id=p.id,
        shots=[{**_shot(0).model_dump(), "prompt_status": "approved"}],
        episode=1, label="第1集",
    )
    j = store.create_video_job(project_id=p.id, shot_id=_shot(0).id,
                               provider="minimax", model="m", prompt="甲起身。", version_id=v.id)
    store.update_video_job(j.id, status="succeeded", video_url="https://x/a.mp4")
    data = episode_statuses(store, p)
    assert data["active"] == 1
    ep1, ep2 = data["episodes"]
    assert ep1["shots"] == 1 and ep1["approved"] == 1 and ep1["generated"] == 1
    assert ep1["preview_url"] == "https://x/a.mp4"
    assert ep1["complete"] is True
    assert ep2["shots"] == 0 and ep2["started"] is False and ep2["complete"] is False
    assert ep2["preview_url"] == ""


def test_旧提示词的成片不能解锁新版当前集(store):
    p = store.create_project(title="t", adaptation_type="short_drama",
                             language="zh-CN", raw_text="原文")
    store.create_version(p, _script(), source_type="generation", label="t", set_current=True)
    original = {**_shot(0).model_dump(), "prompt_status": "approved"}
    old = store.create_video_version(project_id=p.id, shots=[original], episode=1)
    job = store.create_video_job(project_id=p.id, shot_id=original["id"],
                                 provider="minimax", model="m", prompt="甲起身。", version_id=old.id)
    store.update_video_job(job.id, status="succeeded", video_url="https://x/old.mp4")
    assert active_episode_status(store, p)["complete"]

    changed = {**original, "video_prompt": "甲开门。"}
    current = store.create_video_version(project_id=p.id, shots=[changed], episode=1,
                                         parent_version_id=old.id)
    assert not active_episode_status(store, p)["complete"]
    assert episode_statuses(store, p)["episodes"][0]["generated"] == 0

    new_job = store.create_video_job(project_id=p.id, shot_id=changed["id"],
                                     provider="minimax", model="m", prompt="甲开门。",
                                     version_id=current.id)
    store.update_video_job(new_job.id, status="succeeded", video_url="https://x/new.mp4")
    assert active_episode_status(store, p)["complete"]


def test_失败旧任务不能绕过当前提示词审批(store, monkeypatch):
    from fastapi import HTTPException
    from app.video import service

    p = store.create_project(title="t", adaptation_type="short_drama",
                             language="zh-CN", raw_text="原文")
    store.create_version(p, _script(), source_type="generation", label="t", set_current=True)
    original = {**_shot(0).model_dump(), "prompt_status": "approved"}
    old = store.create_video_version(project_id=p.id, shots=[original], episode=1)
    job = store.create_video_job(project_id=p.id, shot_id=original["id"],
                                 provider="minimax", model="m", prompt="甲起身。", version_id=old.id)
    store.update_video_job(job.id, status="failed", error_message="余额不足")
    store.create_video_version(project_id=p.id, episode=1, parent_version_id=old.id,
                               shots=[{**original, "video_prompt": "甲开门。",
                                       "prompt_status": "needs_review"}])
    monkeypatch.setattr(service.deps, "store", lambda: store)
    monkeypatch.setattr(service, "_submit_video_job", lambda _: (_ for _ in ()).throw(
        AssertionError("不应提交旧任务")))

    import pytest
    with pytest.raises(HTTPException) as exc:
        service.retry_video_job(job.id)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        service.reroll_shot(p.id, original["id"])
    assert exc.value.status_code == 409
    assert store.get_video_job(job.id).status == "failed"
