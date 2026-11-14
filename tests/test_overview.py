# =====================================================================
# test_overview.py —— 作品「消耗 / 产出」概览（C7 最小版）
#
# 只聚合已落库的数据，不新增采集点。这组测试锁住的是**口径**——
# 数字算错比没有数字更糟：
#   - 用量（秒数/参考）只算真提交出去的任务（没拿到 external_task_id = 没出门）；
#   - 费用估算不进界面（D-53：预估与实付脱节只会伤信任）；
#   - 镜头完成/失败按「每镜最新一次」判（重 roll 后旧失败不该还挂着）；
#   - 定妆图按素材去重（多角色共用一张不该算两个）；
#   - 全部是创作者语言，响应里不出现 token 字样。
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
    monkeypatch.setattr(api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store))
    return TestClient(create_app())


def _project_with_two_shots(client, store, sample_text):
    """造一个有剧本 + 两镜方案的项目，返回 (pid, [shot_id, shot_id])。"""
    data = client.post(
        "/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"}
    ).json()
    pid = data["id"]
    # 给项目一个剧本版本（概览的场/人数来自它）
    from app.domain import Script

    script = Script.model_validate({
        "title": "雨夜", "logline": "赴约",
        "source": {"chapter_count": 3, "chapter_ids": ["ch_001", "ch_002", "ch_003"]},
        "characters": [
            {"id": "char_a", "name": "林然", "gender": "male"},
            {"id": "char_b", "name": "阿姐", "gender": "female"},
        ],
        "locations": [{"id": "loc_1", "name": "旧楼"}],
        "scenes": [{
            "id": "scene_001", "title": "赴约", "chapter_refs": ["ch_001"],
            "location_id": "loc_1", "characters": ["char_a"],
            "purpose": "见阿姐", "conflict": "不认",
            "beats": [{"id": "beat_001", "type": "action", "text": "停车"}],
        }],
    })
    store.create_version(store.get_project(pid), script, source_type="generation", label="初稿")
    vv = store.create_video_version(
        project_id=pid, source_type="agent",
        shots=[
            {"id": "shot_1", "scene_id": "scene_001", "order": 0, "prompt_status": "approved"},
            {"id": "shot_2", "scene_id": "scene_001", "order": 1, "prompt_status": "needs_review"},
        ],
    )
    return pid, vv


def test_overview_counts_and_no_token_words(client, store, sample_text):
    pid, _ = _project_with_two_shots(client, store, sample_text)
    resp = client.get(f"/api/projects/{pid}/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scenes"] == 1
    assert body["characters"] == 2
    assert body["shots"] == 2
    assert body["shots_approved"] == 1  # 只有 shot_1 批准了
    # 响应体里不得出现 token（D-24：界面禁工程黑话）
    assert "token" not in str(body).lower()


def test_overview_usage_only_counts_submitted(client, store, sample_text):
    """用量聚合（D-53）只累加真提交出去的（有 external_task_id）任务；
    费用估算不再出现在概览响应里。"""
    pid, _ = _project_with_two_shots(client, store, sample_text)
    # 一条已提交（计用量）、一条只落库没出门（不计）
    store.create_video_job(
        project_id=pid, shot_id="shot_1", provider="minimax", model="default",
        prompt="p", params={"usage": {"gen_sec": 6.0, "ref_video_count": 1,
                                       "ref_video_sec": 4.5, "ref_image_count": 2}},
    )
    submitted = store.create_video_job(
        project_id=pid, shot_id="shot_2", provider="minimax", model="default",
        prompt="p", params={"usage": {"gen_sec": 8.0, "ref_video_count": 2,
                                       "ref_video_sec": 5.0, "ref_image_count": 1}},
    )
    store.update_video_job(submitted.id, external_task_id="ext-123")

    body = client.get(f"/api/projects/{pid}/overview").json()
    assert body["video_seconds_planned"] == pytest.approx(8.0)  # 只有出门那条算用量
    assert body["ref_video_seconds"] == pytest.approx(5.0)
    assert body["ref_video_count"] == 2
    assert body["ref_image_count"] == 1
    assert body["video_submissions"] == 1
    assert "cost_estimate_usd" not in body  # 费用不进界面（D-53）


def test_overview_uses_latest_job_per_shot(client, store, sample_text):
    """镜头成功/失败按每镜最新一次任务判——重 roll 成功后旧失败不该还挂着。"""
    pid, _ = _project_with_two_shots(client, store, sample_text)
    old = store.create_video_job(
        project_id=pid, shot_id="shot_1", provider="minimax", model="default",
        prompt="p", params={},
    )
    store.update_video_job(old.id, status="failed", external_task_id="ext-a")
    # 重 roll 成功（新任务，created_at 更新）
    import time
    time.sleep(0.01)
    new = store.create_video_job(
        project_id=pid, shot_id="shot_1", provider="minimax", model="default",
        prompt="p", params={},
    )
    store.update_video_job(new.id, status="succeeded", video_url="http://v.mp4", external_task_id="ext-b")

    body = client.get(f"/api/projects/{pid}/overview").json()
    assert body["shots_done"] == 1
    assert body["shots_failed"] == 0  # 旧失败已被最新成功覆盖


def test_overview_reference_images_dedup(client, store, sample_text):
    """定妆图按素材地址去重：两角色共用一张不该算两个。"""
    pid, _ = _project_with_two_shots(client, store, sample_text)
    store.merge_video_style(pid, {
        "reference_images": {"char_a": "http://img/one.png", "char_b": "http://img/one.png"},
        "voice_casting": {"char_a": "voice-1", "char_b": "voice-1"},
    })
    body = client.get(f"/api/projects/{pid}/overview").json()
    assert body["reference_images"] == 1
    assert body["voices"] == 1


def test_overview_empty_project_has_no_script(store, client, sample_text):
    """没剧本也能查：计数为 0，不报错。"""
    data = client.post("/api/projects/import", data={"raw_text": sample_text, "title": "空"}).json()
    # import 会带一个初始版本，这里直接查不存在的字段口径
    body = client.get(f"/api/projects/{data['id']}/overview").json()
    assert "shots" in body and body["shots"] == 0


def test_overview_404_for_unknown_project(client):
    assert client.get("/api/projects/proj_不存在/overview").status_code == 404
