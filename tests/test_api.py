# =====================================================================
# test_api.py —— REST / SSE 接口层测试（无 RAG 版）
# =====================================================================

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
import app.api.video as video_api
from app.agent.tasks import SubAgentRunner
from app.main import create_app


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    # 路由统一通过 app.api.deps 取值，因此替换这一处即可覆盖全部路由。
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    # 后台任务 runner 也要绑到测试库，否则会去连 .env 里的开发库。
    monkeypatch.setattr(
        api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store)
    )
    return TestClient(create_app())


def _import_project(client, sample_text):
    resp = client.post("/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_import_project_and_list(client, sample_text):
    data = _import_project(client, sample_text)
    assert data["title"] == "雨夜"
    assert data["conversation_id"]
    assert "genres" in data

    projects = client.get("/api/projects").json()
    assert len(projects) == 1
    assert projects[0]["title"] == "雨夜"


def test_video_prompt_review_creates_version_and_gates_batch(client, sample_text):
    project = _import_project(client, sample_text)
    shots = [
        {
            "id": "shot_scene_001_000",
            "scene_id": "scene_001",
            "order": 0,
            "subject": "齐夏",
            "video_prompt": "A close-up shot of Qi Xia looking toward the door.",
            "prompt_status": "needs_review",
        },
    ]
    created = client.post(
        f"/api/projects/{project['id']}/video-versions",
        json={"shots": shots, "label": "Prompt 草稿"},
    )
    assert created.status_code == 200, created.text
    version_id = created.json()["id"]

    reviewed = client.put(
        f"/api/projects/{project['id']}/video-versions/{version_id}/shots/"
        "shot_scene_001_000/prompt",
        json={
            "prompt": "A restrained close-up of Qi Xia turning toward the door.",
            "decision": "approve",
            "note": "保留克制的转身动作，避免多余表演。",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["prompt_status"] == "approved"
    assert reviewed.json()["approved_count"] == 1

    latest = client.get(f"/api/video-versions/{reviewed.json()['version_id']}")
    assert latest.status_code == 200
    assert latest.json()["shots"][0]["prompt_status"] == "approved"
    assert latest.json()["shots"][0]["prompt_review_note"].startswith("保留")

    unapproved = client.post(
        f"/api/projects/{project['id']}/video-versions",
        json={
            "shots": [{**shots[0], "prompt_status": "needs_review"}],
            "parent_version_id": reviewed.json()["version_id"],
            "label": "重新待审阅",
        },
    )
    assert unapproved.status_code == 200
    blocked = client.post(
        f"/api/projects/{project['id']}/video/generate-batch",
        json={"version_id": unapproved.json()["id"]},
    )
    assert blocked.status_code == 400
    assert "批准" in blocked.json()["detail"]


def test_single_video_generation_requires_approved_prompt(client, sample_text, monkeypatch):
    project = _import_project(client, sample_text)
    created = client.post(
        f"/api/projects/{project['id']}/video-versions",
        json={
            "shots": [
                {
                    "id": "shot_scene_001_000",
                    "scene_id": "scene_001",
                    "order": 0,
                    "video_prompt": "A close-up shot of Qi Xia looking toward the door.",
                    "prompt_status": "needs_review",
                },
            ],
            "label": "Prompt 草稿",
        },
    )
    assert created.status_code == 200, created.text

    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)
    blocked = client.post(
        f"/api/projects/{project['id']}/video/generate",
        json={
            "shot_id": "shot_scene_001_000",
            "provider": "minimax",
            "model": "default",
            "prompt": "A close-up shot of Qi Xia looking toward the door.",
            "version_id": created.json()["id"],
        },
    )
    assert blocked.status_code == 409
    assert "尚未批准" in blocked.json()["detail"]


def test_bulk_prompt_review_exposes_version_summary_and_history(client, sample_text):
    project = _import_project(client, sample_text)
    created = client.post(
        f"/api/projects/{project['id']}/video-versions",
        json={
            "shots": [
                {
                    "id": "shot_scene_001_000",
                    "scene_id": "scene_001",
                    "order": 0,
                    "video_prompt": "A quiet close-up by the door.",
                    "prompt_status": "needs_review",
                },
                {
                    "id": "shot_scene_001_001",
                    "scene_id": "scene_001",
                    "order": 1,
                    "video_prompt": "A wide shot of the rainy street.",
                    "prompt_status": "needs_revision",
                },
            ],
            "label": "Prompt 草稿",
        },
    )
    assert created.status_code == 200, created.text
    version_id = created.json()["id"]

    reviewed = client.post(
        f"/api/projects/{project['id']}/video-versions/{version_id}/prompts/review",
        json={
            "shot_ids": ["shot_scene_001_000"],
            "decision": "approve",
            "note": "批量审阅后保留。",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["reviewed_count"] == 1
    assert reviewed.json()["approved_count"] == 1
    assert reviewed.json()["pending_count"] == 1

    versions = client.get(f"/api/projects/{project['id']}/video-versions")
    assert versions.status_code == 200
    latest = versions.json()[0]
    assert latest["current"] is True
    assert latest["prompt_summary"]["review_status"] == "mixed"
    assert latest["prompt_summary"]["approved_count"] == 1

    history = client.get(
        f"/api/projects/{project['id']}/video-versions/{reviewed.json()['version_id']}/"
        "shots/shot_scene_001_000/prompt-history"
    )
    assert history.status_code == 200, history.text
    assert len(history.json()["history"]) == 2
    assert history.json()["history"][0]["prompt_status"] == "approved"
    assert history.json()["history"][0]["prompt_changed"] is False
    assert history.json()["history"][0]["prompt_review_note"] == "批量审阅后保留。"

    edited = client.put(
        f"/api/projects/{project['id']}/video-versions/{reviewed.json()['version_id']}/"
        "shots/shot_scene_001_000/prompt",
        json={
            "prompt": "A restrained close-up by the rain-streaked door.",
            "decision": "save",
            "note": "补充雨痕细节。",
        },
    )
    assert edited.status_code == 200, edited.text
    changed_history = client.get(
        f"/api/projects/{project['id']}/video-versions/{edited.json()['version_id']}/"
        "shots/shot_scene_001_000/prompt-history"
    )
    assert changed_history.status_code == 200
    latest_entry = changed_history.json()["history"][0]
    assert latest_entry["prompt_changed"] is True
    assert any(line.startswith("+A restrained") for line in latest_entry["diff"])


def test_tasks_endpoint_degrades_without_db(monkeypatch):
    """后台任务落库不可用时 /api/tasks 不应 500（退化为纯内存列表）。"""

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(api_mod.deps, "store", _boom)
    monkeypatch.setattr(api_mod.deps, "subagent_runner", lambda: SubAgentRunner())
    resp = TestClient(create_app()).get("/api/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_chat_stream_emits_done_frame(client, sample_text):
    data = _import_project(client, sample_text)
    events = []
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"project_id": data["id"], "message": "帮我看看这部小说"},
    ) as resp:
        assert resp.status_code == 200
        event_name = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event_name = line[len("event: "):].strip()
            elif line.startswith("data: ") and event_name:
                events.append((event_name, json.loads(line[len("data: "):])))
                event_name = None
    names = [name for name, _ in events]
    assert "done" in names, f"SSE 应以 done 事件收尾，实际：{names}"
    done = next(payload for name, payload in events if name == "done")
    assert done["reply"]


def test_video_generation_carries_aspect_ratio(client, sample_text, monkeypatch):
    """画幅选择（9:16 等）随任务落库：单发与批次都写进 params.aspect_ratio。"""
    import app.api.video as video_api

    project = _import_project(client, sample_text)
    created = client.post(
        f"/api/projects/{project['id']}/video-versions",
        json={
            "shots": [
                {
                    "id": "shot_scene_001_000",
                    "scene_id": "scene_001",
                    "order": 0,
                    "video_prompt": "A close-up shot of Qi Xia.",
                    "prompt_status": "approved",
                },
                {
                    "id": "shot_scene_001_001",
                    "scene_id": "scene_001",
                    "order": 1,
                    "video_prompt": "A wide shot of the rainy street.",
                    "prompt_status": "approved",
                },
            ],
            "label": "已批准",
        },
    )
    assert created.status_code == 200, created.text

    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    single = client.post(
        f"/api/projects/{project['id']}/video/generate",
        json={
            "shot_id": "shot_scene_001_000",
            "provider": "minimax",
            "model": "default",
            "prompt": "A close-up shot of Qi Xia.",
            "version_id": created.json()["id"],
            "params": {"aspect_ratio": "9:16"},
        },
    )
    assert single.status_code == 200, single.text

    batch = client.post(
        f"/api/projects/{project['id']}/video/generate-batch",
        json={
            "mode": "auto",
            "provider": "minimax",
            "model": "default",
            "aspect_ratio": "9:16",
        },
    )
    assert batch.status_code == 200, batch.text

    jobs = client.get(f"/api/projects/{project['id']}/video/jobs").json()
    assert len(jobs) >= 2
    assert all(j["params"].get("aspect_ratio") == "9:16" for j in jobs)


# ---------- 场景拖拽排序落库 ----------


def test_scene_order_reorder_persists_as_new_version(client, store, sample_text):
    """拖拽排序必须真实落库为新版本（此前左栏拖拽只改本地状态，刷新即丢）。"""
    from copy import deepcopy

    from app.pipeline.patch import make_source_script

    project = _import_project(client, sample_text)
    p = store.get_project(project["id"])
    script = make_source_script("雨夜", sample_text)
    second = deepcopy(script.scenes[0].model_dump())
    second["id"] = "scene_002"
    second["title"] = "第二场"
    script.scenes.append(type(script.scenes[0]).model_validate(second))
    v = store.create_version(p, script, source_type="generation", label="初始生成", set_current=True)

    resp = client.post(
        f"/api/versions/{v.id}/scene_order",
        json={"scene_ids": ["scene_002", "scene_001"]},
    )
    assert resp.status_code == 200, resp.text
    new_vid = resp.json()["version_id"]
    assert new_vid != v.id
    nv = store.get_version(new_vid)
    assert [s.id for s in nv.script.scenes] == ["scene_002", "scene_001"]
    # 新版本成为当前版本
    assert store.latest_version(store.get_project(project["id"])).id == new_vid

    # 不完整的排列拒绝
    bad = client.post(f"/api/versions/{v.id}/scene_order", json={"scene_ids": ["scene_002"]})
    assert bad.status_code == 400


# ---------- 静态资源 / 导出文件名 / 数据库超时 ----------


def test_export_filename_has_single_version_prefix(client, store, sample_text):
    """导出文件名不该出现「_vver_」：version_id 自己就带 ver_ 前缀。"""
    from app.pipeline.patch import make_source_script

    project = _import_project(client, sample_text)
    p = store.get_project(project["id"])
    v = store.create_version(
        p, make_source_script("雨夜", sample_text),
        source_type="generation", label="初始生成", set_current=True,
    )
    resp = client.get(f"/api/versions/{v.id}/export")
    assert resp.status_code == 200
    disp = resp.headers["content-disposition"]
    assert "vver_" not in disp
    assert f"_{v.id}." in disp


def test_ascii_fallback_filename_is_readable():
    """纯中文标题的 ASCII 退路不该是「____.docx」这种谁也读不出的名字。"""
    from app.api.common import content_disposition

    mixed = content_disposition("雨夜归人_ver_abc123.txt")["Content-Disposition"]
    assert 'filename="ver_abc123.txt"' in mixed  # 有 ASCII 成分就保留
    pure_cn = content_disposition("雨夜归人.docx")["Content-Disposition"]
    assert 'filename="script.docx"' in pure_cn
    assert "filename*=UTF-8''" in pure_cn  # 真名走 RFC 5987


def test_postgres_connect_args_are_bounded():
    """libpq 默认无限等：数据库没起来时请求会永久挂住，必须显式给超时。"""
    from app.store import connect_args_for

    pg = connect_args_for("postgresql+psycopg://u:p@localhost:5432/db")
    assert pg.get("connect_timeout", 0) > 0
    assert connect_args_for("sqlite:///:memory:") == {"check_same_thread": False}


def test_fallback_warning_names_the_real_reason(client, monkeypatch, sample_text):
    """余额耗尽时不能提示「配置 key」——用户早就配了，坏的是余额。

    三种失败（余额 402 / key 失效 401 / 限流 429）都走同一个 local-fallback，
    提示必须能区分，否则用户会去修一个没坏的东西。
    """
    from app.api import projects as projects_api

    def _fallback(error: str):
        def _fake(*args, **kwargs):
            return _fake_script(), {"mode": "local-fallback", "error": error}
        return _fake

    def _fake_script():
        from app.pipeline.patch import make_source_script

        return make_source_script("雨夜", "第一章 雨夜\n老周在修表。", adaptation_type="短剧")

    p = _import_project(client, sample_text)

    cases = {
        "Error code: 402 - {'type': 'insufficient_balance_error'}": "余额不足",
        "Error code: 401 - invalid_api_key": "key 无效",
        "Error code: 429 - rate limit exceeded": "限流",
    }
    for error, expect in cases.items():
        monkeypatch.setattr(projects_api, "generate_script", _fallback(error))
        resp = client.post(f"/api/projects/{p['id']}/generate", json={})
        assert resp.status_code == 200, resp.text
        warnings = " ".join(resp.json().get("warnings") or [])
        assert expect in warnings, f"{error} 应提示「{expect}」，实际：{warnings}"

    # 没配模型（error 为空）时仍然给原来那句
    monkeypatch.setattr(projects_api, "generate_script", _fallback(""))
    resp = client.post(f"/api/projects/{p['id']}/generate", json={})
    assert "未配置可用模型" in " ".join(resp.json().get("warnings") or [])


def test_出片时长用导演定的值而不是写死的六秒(client, sample_text, monkeypatch):
    """真 bug：出片曾一律 `params.get("duration_sec") or 6`。

    导演在分镜里设计的 8–10 秒镜头，只要调用方没显式传时长，就被压成 6 秒送进
    模型——动作硬塞进更短的时间，只能丢节拍、糊成一片。
    优先级：调用方显式指定 > 导演给该镜的 duration_sec > 节奏默认值。
    """
    import app.api.video as video_api

    project = _import_project(client, sample_text)
    created = client.post(
        f"/api/projects/{project['id']}/video-versions",
        json={
            "shots": [{
                "id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
                "duration_sec": 9, "video_prompt": "A close-up.", "prompt_status": "approved",
            }],
            "label": "时长测试",
        },
    )
    assert created.status_code == 200, created.text
    version_id = created.json()["id"]
    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)

    def _submit(params: dict) -> dict:
        r = client.post(
            f"/api/projects/{project['id']}/video/generate",
            json={
                "shot_id": "shot_scene_001_000", "provider": "minimax", "model": "default",
                "prompt": "A close-up.", "version_id": version_id, "params": params,
            },
        )
        assert r.status_code == 200, r.text
        # 提交接口只回 id，落库的 params 要另查一次
        return client.get(f"/api/video/jobs/{r.json()['id']}").json()

    assert _submit({})["params"]["duration_sec"] == 9          # 导演说了算
    assert _submit({"duration_sec": 5})["params"]["duration_sec"] == 5   # 用户可覆盖


def test_节奏默认时长有建议值():
    """拿不到导演时长时的兜底：按节奏档给建议值，而不是随手 6 秒。"""
    from app.video.rhythm import clamp_duration, suggest_duration, suggest_ladder

    assert suggest_duration("slow") > suggest_duration("medium") > suggest_duration("fast")
    assert suggest_duration(None) == suggest_duration("medium")
    assert suggest_duration("乱写") == suggest_duration("medium")
    assert clamp_duration(2) == 4.0
    assert clamp_duration(99) == 15.0
    ladder = suggest_ladder("fast", 4)
    assert len(ladder) == 4 and ladder[0] > ladder[-1]      # 越切越短


# ---------- 清除模型偏好（回落到 .env） ----------


def test_clear_model_preference_falls_back_to_env(client):
    """.env 与界面偏好冲突时，必须有一条正规出口回到 .env。

    背景：界面选的模型优先级高于 .env，一旦选过就一直生效。用户改了 .env
    的模型名却不生效时，没有清除入口只能删库。
    """
    # 先建一个真实厂商（偏好不能指向不存在的厂商——那会变成「看着选了、其实没选」）
    prov = client.post("/api/providers", json={
        "kind": "llm", "name": "demo", "label": "演示厂商",
        "base_url": "https://example.invalid/v1", "api_key": "sk-demo",
        "config": {"model": "demo-model"},
    })
    assert prov.status_code == 200, prov.text
    provider_id = prov.json()["id"]

    # 再写一条界面偏好（模拟「用户在设置里选过模型」）
    resp = client.put("/api/models/preferences", json={
        "task_type": "chat", "provider_id": provider_id, "model_name": "demo-model",
        "is_default": True,
    })
    assert resp.status_code == 200, resp.text
    prefs = client.get("/api/models/preferences").json()
    assert any(p["task_type"] == "chat" and p["model_name"] == "demo-model" for p in prefs)

    # 清除后这条偏好不应再出现
    resp = client.delete("/api/models/preferences/chat")
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] >= 1
    prefs = client.get("/api/models/preferences").json()
    assert not any(p["task_type"] == "chat" for p in prefs)


def test_preference_cannot_point_to_missing_provider(client):
    """偏好指向不存在的厂商要被拒。

    否则它会变成「默认对话模型」，而真正生效的其实回落到 .env——
    界面上看着像选了、其实没选，是最难查的一类状态。
    """
    resp = client.put("/api/models/preferences", json={
        "task_type": "chat", "provider_id": "prov_does_not_exist", "model_name": "ghost",
        "is_default": True,
    })
    assert resp.status_code == 400, resp.text
    assert "厂商不存在" in resp.json()["detail"]


def test_clear_model_preference_is_idempotent(client):
    """清除一个不存在的偏好不该报错（前端可能重复点）。"""
    resp = client.delete("/api/models/preferences/some_unknown_task")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 0
