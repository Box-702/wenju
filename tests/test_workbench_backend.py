# =====================================================================
# test_workbench_backend.py —— 工作台后端补齐（2026-11-20，B1）
#
# 即梦对标的四视图（片段编辑器/单镜重出）需要的两处后端能力：
#   1. 镜头结构化参数写接口：duration_sec / shot_type / camera / dialogue_at
#      （VideoPromptReview 扩字段，None=不动）；改了组内机位时长 →
#      组提示词的「镜头N（X秒）」秒数过期，整组打回待审；
#   2. 单镜重出片端点 POST .../video/shots/{id}/reroll：
#      失败任务重试 / 已批准合法重出 / 未批准 409 / 组感知归到组头。
#      这也是修复工具链（C2「只重出受影响片段」）的底座。
# =====================================================================

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
import app.video.service as video_service
from app.agent.tasks import SubAgentRunner
from app.main import create_app


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    import app.deps as app_deps
    monkeypatch.setattr(app_deps, "store", lambda: store)
    monkeypatch.setattr(app_deps, "settings", lambda: settings)
    monkeypatch.setattr(
        api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store)
    )
    # 重出片会真投递——测试里全部打桩，一条钱都不花
    submitted: list[str] = []
    monkeypatch.setattr(video_service, "_submit_video_job",
                        lambda job: submitted.append(job.id))
    client_obj = TestClient(create_app())
    client_obj._submitted = submitted  # type: ignore[attr-defined]
    return client_obj


def _project(client, sample_text):
    resp = client.post("/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _version(client, project_id, shots, label="方案"):
    resp = client.post(
        f"/api/projects/{project_id}/video-versions",
        json={"shots": shots, "label": label},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _shot(shot_id, order, prompt="A close-up shot of Qi Xia.", **kw):
    d = {"id": shot_id, "scene_id": "scene_001", "order": order,
         "video_prompt": prompt, "prompt_status": "approved",
         "duration_sec": 4.0, "shot_type": "close_up",
         "camera": {"type": "static", "direction": None, "speed": "medium",
                    "path": "", "height": ""}}
    d.update(kw)
    return d


# ---------------------------------------------------------------- 参数写接口


def test_参数写接口_给了就改_none不动(client, sample_text):
    pid = _project(client, sample_text)["id"]
    vid = _version(client, pid, [_shot("shot_scene_001_000", 0)])

    resp = client.put(
        f"/api/projects/{pid}/video-versions/{vid}/shots/shot_scene_001_000/prompt",
        json={"prompt": "A close-up shot of Qi Xia.", "decision": "save",
              "duration_sec": 5.5, "shot_type": "medium"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["params_changed"]) == {"duration_sec", "shot_type"}

    # 新版本里参数确实落了；没给的 dialogue_at/camera 保持原样
    new_vid = body["version_id"]
    detail = client.get(f"/api/video-versions/{new_vid}").json()
    shot = next(s for s in detail["shots"] if s["id"] == "shot_scene_001_000")
    assert shot["duration_sec"] == 5.5 and shot["shot_type"] == "medium"
    assert shot.get("dialogue_at") is None
    assert shot["camera"]["type"] == "static"


def test_空camera对象当没给_不覆盖导演运镜(client, sample_text):
    pid = _project(client, sample_text)["id"]
    vid = _version(client, pid, [_shot(
        "shot_scene_001_000", 0,
        camera={"type": "dolly", "direction": "in", "speed": "slow",
                "path": "向人物推进两米", "height": "1.5m"})])
    resp = client.put(
        f"/api/projects/{pid}/video-versions/{vid}/shots/shot_scene_001_000/prompt",
        json={"prompt": "A close-up shot of Qi Xia.", "decision": "save",
              "camera": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["params_changed"] == []          # 空对象=没改动
    detail = client.get(f"/api/video-versions/{resp.json()['version_id']}").json()
    shot = next(s for s in detail["shots"] if s["id"] == "shot_scene_001_000")
    assert shot["camera"]["type"] == "dolly"            # 原运镜没被默认值覆盖


def test_组内改时长_整组打回待审重折叠(client, sample_text):
    """组提示词里的「镜头N（X秒）」是 DP 按成员时长算的——改了时长，
    秒数就过期；API 层不重折叠，整组打回待审，绝不让用户拿对不上秒数
    的提示词批准出片。"""
    pid = _project(client, sample_text)["id"]
    shots = [
        _shot("shot_scene_001_000", 0, group_id="g1", group_index=0,
              group_prompt="镜头1（4秒）：甲。镜头2（4秒）：乙。"),
        _shot("shot_scene_001_001", 1, group_id="g1", group_index=1),
    ]
    vid = _version(client, pid, shots)
    resp = client.put(
        f"/api/projects/{pid}/video-versions/{vid}/shots/shot_scene_001_001/prompt",
        json={"prompt": "A close-up shot of Qi Xia.", "decision": "save",
              "duration_sec": 6.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "duration_sec" in body["params_changed"]
    assert "重新生成" in body["group_refold_note"]
    detail = client.get(f"/api/video-versions/{body['version_id']}").json()
    statuses = {s["id"]: s["prompt_status"] for s in detail["shots"]}
    assert statuses["shot_scene_001_000"] == "needs_review"
    assert statuses["shot_scene_001_001"] == "needs_review"


# ---------------------------------------------------------------- reroll 端点


def test_reroll_已批准合法重出(client, sample_text):
    pid = _project(client, sample_text)["id"]
    vid = _version(client, pid, [_shot("shot_scene_001_000", 0)])
    resp = client.post(f"/api/projects/{pid}/video/shots/shot_scene_001_000/reroll")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reused"] is False and body["status"] == "submitted"
    assert body["unit_shot_id"] == "shot_scene_001_000"
    assert client._submitted == [body["job_id"]]        # 真投递了（打桩计数）


def test_reroll_失败任务走重试不新建(client, sample_text):
    pid = _project(client, sample_text)["id"]
    vid = _version(client, pid, [_shot("shot_scene_001_000", 0)])
    # 造一条失败任务
    job = client.post(f"/api/projects/{pid}/video/generate", json={
        "shot_id": "shot_scene_001_000", "provider": "minimax", "model": "default",
        "prompt": "A close-up shot of Qi Xia.", "version_id": vid,
    }).json()
    from app.deps import store as get_store
    get_store().update_video_job(job["id"], status="failed", error_message="x")

    resp = client.post(f"/api/projects/{pid}/video/shots/shot_scene_001_000/reroll")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == job["id"]                  # 复用的是原任务
    assert body["status"] == "pending" and body["reused"] is True
    assert "失败" in body["note"]


def test_reroll_未批准409(client, sample_text):
    pid = _project(client, sample_text)["id"]
    vid = _version(client, pid, [_shot("shot_scene_001_000", 0,
                                       prompt_status="needs_review")])
    resp = client.post(f"/api/projects/{pid}/video/shots/shot_scene_001_000/reroll")
    assert resp.status_code == 409
    assert "批准" in resp.json()["detail"]


def test_reroll_组内机位归到组头单元(client, sample_text):
    """提交单位是整段片段：对组内第二机位发起 reroll，实际重出的是组头任务，
    note 用人话说清楚。"""
    pid = _project(client, sample_text)["id"]
    shots = [
        _shot("shot_scene_001_000", 0, group_id="g1", group_index=0,
              group_prompt="镜头1（4秒）：甲。镜头2（4秒）：乙。"),
        _shot("shot_scene_001_001", 1, group_id="g1", group_index=1),
    ]
    vid = _version(client, pid, shots)
    resp = client.post(f"/api/projects/{pid}/video/shots/shot_scene_001_001/reroll")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unit_shot_id"] == "shot_scene_001_000"   # 归到组头
    assert "整段片段" in body["note"]


def test_reroll_不存在的镜头404(client, sample_text):
    pid = _project(client, sample_text)["id"]
    _version(client, pid, [_shot("shot_scene_001_000", 0)])
    resp = client.post(f"/api/projects/{pid}/video/shots/shot_scene_009_009/reroll")
    assert resp.status_code == 404
