# =====================================================================
# test_video_submit_group.py —— 镜头组的提交口径（组头 409 真 bug 的回归锁）
#
# 背景：镜头组 = 一次生成 = 一条视频里的几个切镜。组头持有折叠后的
# 「组提示词」（group_prompt），那才是最终提交给视频模型的原文；
# 组头的 video_prompt 只是它自己那一机位的单镜版本，不会被提交。
#
# 曾经的错：对话工具与前端传的是组头的单镜提示词，服务端的等值校验比的
# 却是组提示词——「指定镜头出片」在任何有镜头组的作品上必然 409。
# 更糟的是即便绕过，用户批准的是 A、系统发出去的是 B。
#
# 本组用例锁死：
#   1. 组头提交用组提示词 → 放行；
#   2. 组头提交用单镜提示词 → 仍被等值校验挡住（改口径不放越权路径）；
#   3. 组内有机位被标「需修改」→ 整段不投（批了半段戏不能发出去）；
#   4. 对话工具按生成单元提交（一段戏只建一个任务，不 N 倍计费）；
#   5. 编辑器保存写回正确字段（组头写 group_prompt，不动 video_prompt）。
# =====================================================================

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
import app.video.service as video_service
from app.agent.tasks import SubAgentRunner
from app.chat import build_chat_tools
from app.main import create_app
from app.video.batch import BatchManager


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    # 出片业务已下沉到 app/video/service.py，它直接读 app.deps（全项目装配点）——
    # api.deps 只是薄转发，两处名字独立，必须都替换。
    import app.deps as app_deps

    monkeypatch.setattr(app_deps, "store", lambda: store)
    monkeypatch.setattr(app_deps, "settings", lambda: settings)
    monkeypatch.setattr(app_deps, "video_manager", api_mod.deps.video_manager)
    batch_manager = BatchManager()
    monkeypatch.setattr(api_mod.deps, "batch_manager", lambda: batch_manager)
    monkeypatch.setattr(
        api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store)
    )
    return TestClient(create_app())


GROUP_PROMPT = (
    "环境锚。人物锚。[Shot 1] 0-4s，排钱。"
    "[Shot 2] At 00:04.000, cuts to 掌柜打量。风格段。"
)


def _group_shots(member_status: str = "needs_review") -> list[dict]:
    """一段戏：一个组头（带组提示词）+ 一个组员（只有自己的单镜提示词）。"""
    return [
        {
            "id": "shot_a_000", "scene_id": "scene_001", "order": 0,
            "duration_sec": 4, "video_prompt": "P0 单镜提示词",
            "group_id": "g1", "group_index": 0,
            "group_prompt": GROUP_PROMPT,
            "prompt_status": "approved",
        },
        {
            "id": "shot_a_001", "scene_id": "scene_001", "order": 1,
            "duration_sec": 3, "video_prompt": "P1 单镜提示词",
            "group_id": "g1", "group_index": 1,
            "prompt_status": member_status,
        },
    ]


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


def _submit(client, project_id, version_id, shot_id, prompt):
    return client.post(
        f"/api/projects/{project_id}/video/generate",
        json={
            "shot_id": shot_id, "provider": "minimax", "model": "default",
            "prompt": prompt, "version_id": version_id,
        },
    )


# ---------- 口径本体 ----------


def test_unit_prompt_组头取组提示词组员取自己的():
    """组头送去生成的是折叠后的组提示词；组员没有组提示词，用自己的。"""
    from app.video.service import unit_prompt

    head, member = _group_shots()
    assert unit_prompt(head) == GROUP_PROMPT
    assert unit_prompt(member) == "P1 单镜提示词"


def test_unit_approved_组内有需修改则整段不投():
    """组头批了但组员被标「需修改」→ 整段不投：批了半段戏就发出去等于替用户做主。"""
    from app.video.service import generation_units, unit_approved

    shots = _group_shots(member_status="needs_revision")
    unit = generation_units(shots)[0]
    assert unit["group_id"] == "g1"
    assert not unit_approved(shots, unit)

    shots_ok = _group_shots(member_status="needs_review")
    assert unit_approved(shots_ok, generation_units(shots_ok)[0])


# ---------- REST 提交闸门 ----------


def test_submit_group_head_用组提示词放行(client, sample_text, monkeypatch):
    """组头提交用组提示词 → 放行（这是修复前的 409 现场）。"""
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)
    project = _project(client, sample_text)
    version_id = _version(client, project["id"], _group_shots())

    resp = _submit(client, project["id"], version_id, "shot_a_000", GROUP_PROMPT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reused"] is False


def test_submit_group_head_用单镜提示词仍被挡(client, sample_text, monkeypatch):
    """组头提交单镜提示词 → 仍 409。改口径不能顺手把越权路径放开。"""
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)
    project = _project(client, sample_text)
    version_id = _version(client, project["id"], _group_shots())

    resp = _submit(client, project["id"], version_id, "shot_a_000", "P0 单镜提示词")
    assert resp.status_code == 409, resp.text
    assert "不一致" in resp.json()["detail"]


def test_submit_group_组内有需修改则不投(client, sample_text, monkeypatch):
    """组员被标「需修改」→ 整段拒发。"""
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)
    project = _project(client, sample_text)
    version_id = _version(client, project["id"], _group_shots(member_status="needs_revision"))

    resp = _submit(client, project["id"], version_id, "shot_a_000", GROUP_PROMPT)
    assert resp.status_code == 409, resp.text
    assert "需要修改" in resp.json()["detail"]


# ---------- 对话工具 ----------


def test_submit_videos_指定镜头按组提交只建一个任务(store, llm, sample_text, monkeypatch):
    """指定镜头出片：按生成单元投组头，一段戏只建一个任务（不 N 倍计费）。"""
    import app.deps as app_deps

    monkeypatch.setattr(app_deps, "store", lambda: store)
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)

    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    tools["create_project"].invoke(
        {"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text}
    )
    pid = collector["project_id"]
    store.create_video_version(
        project_id=pid, shots=_group_shots(), source_type="agent",
    )

    # 指定的是组员——生成单元是整段，所以要投组头。
    r = tools["submit_videos"].invoke({"project_id": pid, "shot_ids": "shot_a_001"})
    assert "已提交" in r, r

    jobs = store.list_video_jobs(pid)
    assert len(jobs) == 1, f"一段戏建了 {len(jobs)} 个任务（会 N 倍计费）"
    assert jobs[0].shot_id == "shot_a_000", "应投组头"
    assert jobs[0].prompt == GROUP_PROMPT, "投出去的应是组提示词"


def test_submit_videos_未批准仍拒发(store, llm, sample_text, monkeypatch):
    """组头没批准 → 仍然拒发（闸门没被口径改动带松）。"""
    import app.deps as app_deps

    monkeypatch.setattr(app_deps, "store", lambda: store)
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)

    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    tools["create_project"].invoke(
        {"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text}
    )
    pid = collector["project_id"]
    shots = _group_shots()
    shots[0]["prompt_status"] = "needs_review"
    store.create_video_version(project_id=pid, shots=shots, source_type="agent")

    r = tools["submit_videos"].invoke({"project_id": pid, "shot_ids": "shot_a_000"})
    assert "需要先经过人工批准" in r, r
    assert not store.list_video_jobs(pid)


# ---------- 编辑器保存写回正确字段 ----------


def test_review_prompt_组头保存写回组提示词字段(client, sample_text):
    """组头编辑提示词 → 写回 group_prompt，不动 video_prompt（写错字段提交时必 409）。"""
    project = _project(client, sample_text)
    version_id = _version(client, project["id"], _group_shots())

    edited = GROUP_PROMPT + "改过的尾巴。"
    resp = client.put(
        f"/api/projects/{project['id']}/video-versions/{version_id}"
        f"/shots/shot_a_000/prompt",
        json={"prompt": edited, "decision": "approve"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["prompt_field"] == "group_prompt"

    # 保存会生成新版本（可回滚），要看新版本里的内容。
    saved = client.get(f"/api/video-versions/{resp.json()['version_id']}").json()
    wanted = next(s for s in saved["shots"] if s["id"] == "shot_a_000")
    assert wanted["group_prompt"] == edited, "编辑结果应落在组提示词字段"
    assert wanted["video_prompt"] == "P0 单镜提示词", "单镜提示词不该被组提示词覆盖"


# ---------- 分集批量 ----------


def test_batch_filter_by_episode(client, sample_text, monkeypatch):
    """带 episode_id 的批量只投本集镜头（分集总览的「批量出片」）。"""
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)
    project = _project(client, sample_text)
    shots = [
        {"id": "shot_e1_000", "scene_id": "scene_001", "order": 0,
         "duration_sec": 2, "video_prompt": "第一集镜头。",
         "prompt_status": "approved", "episode_id": "ep_001"},
        {"id": "shot_e2_000", "scene_id": "scene_002", "order": 1,
         "duration_sec": 2, "video_prompt": "第二集镜头。",
         "prompt_status": "approved", "episode_id": "ep_002"},
    ]
    _version(client, project["id"], shots)

    resp = client.post(
        f"/api/projects/{project['id']}/video/generate-batch",
        json={"mode": "auto", "provider": "minimax", "model": "default",
              "episode_id": "ep_002"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["shot_ids"] == ["shot_e2_000"]


def test_batch_legacy_without_episode_ids_ignores_filter(client, sample_text, monkeypatch):
    """老作品没有 episode_id：分集批量不能把整部作品挡掉。"""
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)
    project = _project(client, sample_text)
    shots = [
        {"id": "shot_old_000", "scene_id": "scene_001", "order": 0,
         "duration_sec": 2, "video_prompt": "老镜头一。", "prompt_status": "approved"},
        {"id": "shot_old_001", "scene_id": "scene_002", "order": 1,
         "duration_sec": 2, "video_prompt": "老镜头二。", "prompt_status": "approved"},
    ]
    _version(client, project["id"], shots)

    resp = client.post(
        f"/api/projects/{project['id']}/video/generate-batch",
        json={"mode": "auto", "provider": "minimax", "model": "default",
              "episode_id": "ep_1"},
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["shot_ids"]) == {"shot_old_000", "shot_old_001"}


def test_review_prompt_单镜保存写回单镜字段(client, sample_text):
    """单镜（非组头）编辑提示词 → 仍然写回 video_prompt。"""
    project = _project(client, sample_text)
    version_id = _version(client, project["id"], _group_shots())

    resp = client.put(
        f"/api/projects/{project['id']}/video-versions/{version_id}"
        f"/shots/shot_a_001/prompt",
        json={"prompt": "改过的单镜提示词", "decision": "save"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["prompt_field"] == "video_prompt"
