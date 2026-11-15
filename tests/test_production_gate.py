# =====================================================================
# test_production_gate.py —— 生产流水线的预算闸门与限场续跑（S4）
#
# 一键跑完生产流水线要花钱（模型调用 + 逐张生图），所以 Agent 只能**发起**，
# 真正开跑由用户点确认卡触发——服务端硬闸门，模型伪造不了（同 D-43）。
# 报的是**确定的事实**（复用哪些阶段、要补几张图），不是报价（D-53）。
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


def _script():
    from app.domain import Script

    return Script(
        title="排钱", logline="排钱", themes=["体面"],
        characters=[{"id": "char_ke", "name": "孔乙己"}],
        locations=[{"id": "loc_01", "name": "咸亨酒店"}],
        scenes=[
            {
                "id": "scene_001", "title": "排钱", "chapter_refs": ["ch_001"],
                "location_id": "loc_01", "characters": ["char_ke"],
                "purpose": "排钱", "conflict": "体面与铜钱",
            },
            {
                "id": "scene_002", "title": "掌柜", "chapter_refs": ["ch_001"],
                "location_id": "loc_01", "characters": ["char_ke"],
                "purpose": "打量", "conflict": "体面与铜钱",
            },
        ],
        source={"chapter_count": 1, "chapter_ids": ["ch_001"]},
    )


def _project_with_script(store):
    project = store.create_project(
        title="排钱", adaptation_type="short_drama", language="zh-CN", raw_text="原文",
    )
    store.create_version(project, _script(), source_type="generation", label="初稿")
    return project


# ---------- 预估 ----------


def test_预估_全新作品全部要跑(store):
    from app.agent.production import estimate_production

    project = store.create_project(
        title="排钱", adaptation_type="short_drama", language="zh-CN", raw_text="原文",
    )
    est = estimate_production(store, project.id)
    actions = {s["key"]: s["action"] for s in est["stages"]}
    assert actions["script"] == "run"
    assert actions["director"] == "run"
    assert actions["style"] == "run"
    assert actions["refs"] == "run"
    # 还没有剧本：角色/场景的个数都不知道，不能编一个数字出来
    assert est["images_to_make"] == 0
    assert "才知道" in next(s for s in est["stages"] if s["key"] == "refs")["detail"]


def test_预估_已有的阶段标复用(store):
    from app.agent.production import estimate_production

    project = _project_with_script(store)
    store.merge_video_style(project.id, {
        "character_appearances": {"char_ke": "短发灰衣"},
        "environment_descriptions": {"loc_01": "昏黄路灯"},
    })
    est = estimate_production(store, project.id)
    actions = {s["key"]: s["action"] for s in est["stages"]}
    assert actions["script"] == "reuse"
    assert actions["style"] == "reuse"
    assert actions["director"] == "run", "没拆过镜还是要拆"


def test_预估_已有镜头方案时报出片资源量(store):
    from app.agent.production import estimate_production

    project = _project_with_script(store)
    store.create_video_version(
        project_id=project.id,
        shots=[
            {"id": "shot_a_000", "scene_id": "scene_001", "order": 0, "duration_sec": 4,
             "group_id": "g1", "group_index": 0},
            {"id": "shot_a_001", "scene_id": "scene_001", "order": 1, "duration_sec": 3,
             "group_id": "g1", "group_index": 1},
            {"id": "shot_a_002", "scene_id": "scene_001", "order": 2, "duration_sec": 5},
        ],
        source_type="agent",
    )
    est = estimate_production(store, project.id)
    assert est["film_units"] == 2, "两个机位算一段，单镜算一段"
    assert est["film_seconds"] == 12


# ---------- 闸门 ----------


def test_start_production_只发起不执行(store, llm, monkeypatch):
    """Agent 只能发起——真正开跑要用户点确认卡。"""
    import app.deps as app_deps
    import app.agent.production as production_mod

    called = []
    monkeypatch.setattr(app_deps, "store", lambda: store)
    monkeypatch.setattr(
        production_mod, "run_production",
        lambda *a, **k: called.append(1) or {"notes": ["跑了"]},
    )
    from app.chat import build_chat_tools

    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    tools["create_project"].invoke({
        "title": "排钱", "adaptation_type": "short_drama", "raw_text": "原文",
    })
    pid = collector["project_id"]

    out = tools["start_production"].invoke({"project_id": pid})
    assert "确认卡" in out
    assert not called, "闸门没过就跑起来了"

    payload = next(p for p in collector["payloads"] if p["type"] == "production_budget")
    assert payload["approval_id"]
    # 服务端留痕：一条 pending 的确认请求
    row = store.get_pending_approval(payload["approval_id"])
    assert row is not None
    assert row.status == "pending"
    assert row.note == "production"


def test_确认后流水线才跑(client, store, monkeypatch):
    import app.agent.production as production_mod

    called = []
    monkeypatch.setattr(
        production_mod, "run_production",
        lambda *a, **k: called.append(k.get("limit", 0)) or {"notes": ["跑完了"]},
    )
    project = _project_with_script(store)
    # 同（作品, 会话）只留一条 pending：先建提示词那条，再建生产这条
    store.create_pending_approval(
        project_id=project.id, conversation_id="", version_id="v1",
        shot_ids=["shot_a"], note="",
    )
    approval = store.create_pending_approval(
        project_id=project.id, conversation_id="", version_id="",
        shot_ids=["limit=2"], note="production",
    )

    r = client.post(f"/api/production/approvals/{approval.id}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert called == [2], "限场数要传下去"
    assert store.get_pending_approval(approval.id).status == "confirmed"


def test_确认请求过期或类型不对要拒(client, store):
    project = _project_with_script(store)
    # 类型不对：这是提示词批准，不是生产流水线
    other = store.create_pending_approval(
        project_id=project.id, conversation_id="", version_id="v1",
        shot_ids=["shot_a"], note="",
    )
    r = client.post(f"/api/production/approvals/{other.id}/confirm")
    assert r.status_code == 400, r.text
    approval = store.create_pending_approval(
        project_id=project.id, conversation_id="", version_id="",
        shot_ids=[], note="production",
    )

    # 重复确认
    assert client.post(f"/api/production/approvals/{approval.id}/confirm").status_code == 200
    assert client.post(f"/api/production/approvals/{approval.id}/confirm").status_code == 409


def test_状态接口带预估(client, store):
    project = _project_with_script(store)
    body = client.get(f"/api/projects/{project.id}/production").json()
    assert "estimate" in body
    assert body["estimate"]["images_to_make"] == 2
