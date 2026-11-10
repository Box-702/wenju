# =====================================================================
# test_video_idempotency.py —— 视频生成提交的幂等闸门
#
# 背景：提交视频生成是真花钱的动作。此前同一镜头可以被重复提交——
# 模型工具重试、用户连点两下、批次与单镜同时提交同一条镜头——
# 每次都会新建任务、重复计费。这组测试锁住幂等行为。
#
# 幂等的边界：只看「在途」（pending/submitting/queued/generating）。
# 上一版已出片的镜头再次提交是合法的「重出片」，不能被挡住。
# =====================================================================

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
import app.api.video as video_api
from app.agent.tasks import SubAgentRunner
from app.main import create_app
from app.video.batch import BatchManager


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    monkeypatch.setattr(
        api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store)
    )
    # 批次管理器是进程级单例（生产里靠 lru_cache）；这里给一份新的，
    # 免得用例之间互相污染——注意要返回同一个实例，不是每次新建。
    batch_manager = BatchManager()
    monkeypatch.setattr(api_mod.deps, "batch_manager", lambda: batch_manager)
    return TestClient(create_app())


def _project(client, sample_text):
    resp = client.post("/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _version(client, project_id, shots):
    resp = client.post(
        f"/api/projects/{project_id}/video-versions",
        json={"shots": shots, "label": "已批准"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _shot(shot_id: str, prompt: str = "A close-up shot of Qi Xia.") -> dict:
    return {
        "id": shot_id,
        "scene_id": "scene_001",
        "order": 0,
        "video_prompt": prompt,
        "prompt_status": "approved",
    }


def _submit(client, project_id, version_id, shot_id, **extra):
    body = {
        "shot_id": shot_id,
        "provider": "minimax",
        "model": "default",
        "prompt": "A close-up shot of Qi Xia.",
        "version_id": version_id,
    }
    body.update(extra)
    return client.post(f"/api/projects/{project_id}/video/generate", json=body)


# ---------- 幂等本体 ----------


def test_repeat_submit_does_not_create_second_job(client, sample_text, monkeypatch):
    """同一镜头重复提交只建一个任务——第二次返回既有任务（reused=True）。"""
    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    project = _project(client, sample_text)
    version_id = _version(client, project["id"], [_shot("shot_a")])

    first = _submit(client, project["id"], version_id, "shot_a")
    assert first.status_code == 200, first.text
    assert first.json()["reused"] is False

    second = _submit(client, project["id"], version_id, "shot_a")
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["reused"] is True
    assert body["id"] == first.json()["id"], "重复提交应返回同一个任务"

    jobs = client.get(f"/api/projects/{project['id']}/video/jobs").json()
    assert len(jobs) == 1, f"重复提交建了 {len(jobs)} 个任务（会重复计费）"


def test_submit_after_success_creates_new_job(client, sample_text, monkeypatch):
    """上一版已出片的镜头再次提交是合法的「重出片」，不能被幂等挡住。"""
    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    project = _project(client, sample_text)
    version_id = _version(client, project["id"], [_shot("shot_a")])

    first = _submit(client, project["id"], version_id, "shot_a")
    job_id = first.json()["id"]

    # 任务跑到终态（出片成功）
    client_store = api_mod.deps.store()
    client_store.update_video_job(job_id, status="succeeded", video_url="http://v.mp4")

    again = _submit(client, project["id"], version_id, "shot_a")
    assert again.status_code == 200, again.text
    assert again.json()["reused"] is False
    assert again.json()["id"] != job_id

    jobs = client.get(f"/api/projects/{project['id']}/video/jobs").json()
    assert len(jobs) == 2


def test_different_shots_are_not_deduplicated(client, sample_text, monkeypatch):
    """幂等只针对同一镜头：不同镜头各建各的任务。"""
    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    project = _project(client, sample_text)
    version_id = _version(
        client, project["id"], [_shot("shot_a"), _shot("shot_b", "A wide shot of the street.")]
    )

    assert _submit(client, project["id"], version_id, "shot_a").status_code == 200
    assert _submit(client, project["id"], version_id, "shot_b").status_code == 200

    jobs = client.get(f"/api/projects/{project['id']}/video/jobs").json()
    assert len(jobs) == 2


def test_changed_params_create_new_job(client, sample_text, monkeypatch):
    """改了参数再提交是有意重出片，不能被幂等吞掉。

    反过来也成立：显式传的时长**等于**默认建议值时，两次请求是等价的，
    仍然算「同一件事」——幂等比的是最终参数，不是字面参数。
    """
    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    project = _project(client, sample_text)
    version_id = _version(client, project["id"], [_shot("shot_a")])

    first = _submit(client, project["id"], version_id, "shot_a")
    assert first.json()["reused"] is False

    # 换个明显不同的时长 → 新任务
    again = _submit(client, project["id"], version_id, "shot_a", params={"duration_sec": 12})
    assert again.json()["reused"] is False
    assert again.json()["id"] != first.json()["id"]

    jobs = client.get(f"/api/projects/{project['id']}/video/jobs").json()
    assert len(jobs) == 2


def test_equal_effective_params_are_deduplicated(client, sample_text, monkeypatch):
    """两次请求最终参数相同 → 视为同一件事，只建一个任务。"""
    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    project = _project(client, sample_text)
    version_id = _version(client, project["id"], [_shot("shot_a")])

    first = _submit(client, project["id"], version_id, "shot_a")
    assert first.json()["reused"] is False

    # 显式把分辨率写成默认值 → 最终参数与第一次一致
    job = client.get(f"/api/video/jobs/{first.json()['id']}").json()
    again = _submit(
        client, project["id"], version_id, "shot_a",
        params={"resolution": job["params"]["resolution"]},
    )
    assert again.json()["reused"] is True
    assert again.json()["id"] == first.json()["id"]

    jobs = client.get(f"/api/projects/{project['id']}/video/jobs").json()
    assert len(jobs) == 1


def test_changed_reference_videos_create_new_job(client, sample_text, monkeypatch):
    """换了参考素材再提交也是重出片——不能拿旧任务糊弄过去。"""
    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    project = _project(client, sample_text)
    version_id = _version(client, project["id"], [_shot("shot_a")])

    first = _submit(client, project["id"], version_id, "shot_a")
    assert first.json()["reused"] is False

    again = _submit(
        client, project["id"], version_id, "shot_a",
        params={"reference_videos": ["asset_vid_1"]},
    )
    assert again.json()["reused"] is False
    assert again.json()["id"] != first.json()["id"]


def test_nonclaim_inflight_job_is_reused(client, sample_text, monkeypatch):
    """非 claim 路径建的在途任务（成片质检测 roll 就直建不占 claim），
    用户再点「出片」时必须复用，不能第三次建档让同一镜头两条任务并行烧钱。"""
    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    project = _project(client, sample_text)
    version_id = _version(client, project["id"], [_shot("shot_a")])

    first = _submit(client, project["id"], version_id, "shot_a")
    job_a = first.json()["id"]

    # 模拟质检重 roll：A 成功 → 直建 B（params 带上 qa_attempt，不占 claim）
    store = api_mod.deps.store()
    store.update_video_job(job_a, status="succeeded", video_url="http://v.mp4")
    job_a_params = dict(store.get_video_job(job_a).params)
    job_b = store.create_video_job(
        project_id=project["id"], shot_id="shot_a", provider="minimax",
        model="default", prompt="A close-up shot of Qi Xia.",
        params={**job_a_params, "qa_attempt": 1}, version_id=version_id,
    )

    # 用户此时再点出片：应复用 B，而不是新建第三条
    again = _submit(client, project["id"], version_id, "shot_a")
    assert again.json()["reused"] is True
    assert again.json()["id"] == job_b.id

    jobs = client.get(f"/api/projects/{project['id']}/video/jobs").json()
    assert len(jobs) == 2, f"同意图复用时不该建第三条任务，实际 {len(jobs)} 条"

    # 复用路径不留残余 claim
    key = store.video_claim_key(
        project["id"], "shot_a",
        video_api._video_intent(
            video_api.VideoJobCreate(
                shot_id="shot_a", provider="minimax", model="default",
                prompt="A close-up shot of Qi Xia.", version_id=version_id,
            ),
            job_a_params, [],
        ),
    )
    assert store.find_video_claim(key) is None


# ---------- claim 生命周期（存储层）----------


def test_claim_blocks_concurrent_window(store):
    """「claim 了但还没建任务」的缝里，第二次占位必须拒绝重放。"""
    project = store.create_project(title="雨夜", adaptation_type="short_drama", language="zh", raw_text="x")
    key = store.video_claim_key(project.id, "shot_a", {"prompt": "p"})
    outcome, task_id = store.claim_video_job(key, project_id=project.id, shot_id="shot_a")
    assert outcome == "claimed" and task_id is None

    outcome2, task_id2 = store.claim_video_job(key, project_id=project.id, shot_id="shot_a")
    assert outcome2 == "replay" and task_id2 is None

    # 建任务 + 落定 → 第三次命中既有任务（复用）
    job = store.create_video_job(
        project_id=project.id, shot_id="shot_a", provider="minimax", model="default", prompt="p"
    )
    store.finish_video_claim(key, job.id)
    outcome3, task_id3 = store.claim_video_job(key, project_id=project.id, shot_id="shot_a")
    assert outcome3 == "replay" and task_id3 == job.id


def test_claim_released_when_job_terminal(store):
    """任务进终态 → claim 自动释放 → 同意图可再次占位。"""
    project = store.create_project(title="雨夜", adaptation_type="short_drama", language="zh", raw_text="x")
    key = store.video_claim_key(project.id, "shot_a", {"prompt": "p"})
    store.claim_video_job(key, project_id=project.id, shot_id="shot_a")
    job = store.create_video_job(
        project_id=project.id, shot_id="shot_a", provider="minimax", model="default", prompt="p"
    )
    store.finish_video_claim(key, job.id)

    # 还没终态：重放被挡（复用既有）
    assert store.claim_video_job(key, project_id=project.id, shot_id="shot_a")[0] == "replay"

    store.update_video_job(job.id, status="succeeded", video_url="http://v.mp4")
    assert store.find_video_claim(key) is None
    outcome, _ = store.claim_video_job(key, project_id=project.id, shot_id="shot_a")
    assert outcome == "claimed"


def test_stale_claim_self_heals(store):
    """崩溃留下的空 claim（超过 10 分钟）自动被清理，新轮正常占到。"""
    from datetime import timedelta

    from app.store import VideoJobClaim, _utcnow_naive

    project = store.create_project(title="雨夜", adaptation_type="short_drama", language="zh", raw_text="x")
    key = store.video_claim_key(project.id, "shot_a", {"prompt": "p"})
    with store.session() as s:
        s.add(VideoJobClaim(key=key, project_id=project.id, shot_id="shot_a"))
        s.commit()
        c = s.get(VideoJobClaim, key)
        c.created_at = _utcnow_naive() - timedelta(minutes=11)
        s.commit()

    outcome, task_id = store.claim_video_job(key, project_id=project.id, shot_id="shot_a")
    assert outcome == "claimed" and task_id is None


def test_claim_key_is_stable_for_equal_intents(store):
    """空参数归一：同键——「没传」与「传了空」不能算两种意图。"""
    k1 = store.video_claim_key("proj", "shot", {"prompt": "p", "params": {}})
    k2 = store.video_claim_key("proj", "shot", {"prompt": "p", "params": {"resolution": None, "content": []}})
    k3 = store.video_claim_key("proj", "shot", {"prompt": "p", "params": {"duration_sec": 5}})
    assert k1 == k2
    assert k1 != k3
    # 与顺序无关（sort_keys）
    k4 = store.video_claim_key("proj", "shot", {"params": {"duration_sec": 5}, "prompt": "p"})
    assert k3 == k4


def test_find_active_video_job_ignores_terminal_jobs(store):
    """在途查得到、终态查不到——这是幂等的边界。"""
    project = store.create_project(title="雨夜", adaptation_type="short_drama", language="zh", raw_text="x")
    job = store.create_video_job(
        project_id=project.id, shot_id="shot_a", provider="minimax",
        model="default", prompt="p",
    )
    assert job.status == "pending"
    assert store.find_active_video_job(project.id, "shot_a") is not None

    store.update_video_job(job.id, status="succeeded", video_url="http://v.mp4")
    assert store.find_active_video_job(project.id, "shot_a") is None

    # 别的镜头不串
    store.update_video_job(job.id, status="pending")
    assert store.find_active_video_job(project.id, "shot_b") is None


def test_find_active_video_job_empty_args(store):
    """空 project / 空 shot 直接返回 None，不查库。"""
    assert store.find_active_video_job("", "shot_a") is None
    assert store.find_active_video_job("proj_x", "") is None


# ---------- 批次闸门 ----------


def test_second_batch_is_rejected_while_one_is_running(client, sample_text, monkeypatch):
    """同一项目同时只允许一个批次在推进——两个批次会重复提交同一批镜头。"""
    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    project = _project(client, sample_text)
    _version(client, project["id"], [_shot("shot_a"), _shot("shot_b", "A wide shot.")])

    first = client.post(
        f"/api/projects/{project['id']}/video/generate-batch",
        json={"mode": "auto", "provider": "minimax", "model": "default"},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/api/projects/{project['id']}/video/generate-batch",
        json={"mode": "auto", "provider": "minimax", "model": "default"},
    )
    assert second.status_code == 409, second.text
    assert "重复计费" in second.json()["detail"]
