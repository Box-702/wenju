# =====================================================================
# test_new_tools.py —— 新增对话工具的行为锁
#
#   search_history  结构化引用历史对话（C2）
#   search_assets   按名字/类型搜资产（M2-6a）
#   save_asset      把图片素材存成角色/场景的参考图（M2-6a）
#   attach_asset    把素材挂到镜头上（M2-6a）
#   generate_video  单镜出片，缺风格/比例时先问（M3-8）
#   submit_videos   提交时挂参考素材（C3）
# =====================================================================

from __future__ import annotations

import pytest


def _ctx(store, llm, conversation_id=None):
    from app.chat.tools.context import Collector, ToolContext

    collector = Collector(project_id=None, conversation_id=conversation_id, payloads=[])
    return ToolContext(store=store, llm=llm, collector=collector), collector


def _tools(store, llm, conversation_id=None):
    from app.chat import build_chat_tools

    ctx, collector = _ctx(store, llm, conversation_id)
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    return tools, collector


def _project(store, title="雨夜"):
    return store.create_project(
        title=title, adaptation_type="short_drama", language="zh-CN", raw_text="原文",
    )


def _version(store, project_id, shots):
    return store.create_video_version(
        project_id=project_id, shots=shots, source_type="agent",
    )


def _approved_shot(sid="shot_a_000", group_id="", group_prompt=None):
    shot = {
        "id": sid, "scene_id": "scene_001", "order": 0, "duration_sec": 4,
        "video_prompt": "P0 一条单镜提示词", "prompt_status": "approved",
    }
    if group_id:
        shot["group_id"] = group_id
        shot["group_index"] = 0
    if group_prompt is not None:
        shot["group_prompt"] = group_prompt
    return shot


# ---------- search_history ----------


def test_search_history_返回带id的条目(store, llm):
    conv = store.create_conversation(title="对", project_id=None)
    store.save_chat_message(thread_id=conv.id, role="user", content="孔乙己是跛脚")
    store.save_chat_message(thread_id=conv.id, role="assistant", content="记住了")
    tools, _ = _tools(store, llm, conversation_id=conv.id)

    out = tools["search_history"].invoke({"keyword": "跛脚"})
    assert "跛脚" in out
    assert "记住了" not in out, "关键词过滤要真的过滤"
    assert "[" in out, "条目要带可引用的 id"


def test_search_history_留空取最近几条(store, llm):
    conv = store.create_conversation(title="对", project_id=None)
    for i in range(5):
        store.save_chat_message(thread_id=conv.id, role="user", content=f"第{i}句")
    tools, _ = _tools(store, llm, conversation_id=conv.id)

    out = tools["search_history"].invoke({"keyword": "", "limit": 2})
    assert "第4句" in out and "第3句" in out
    assert "第0句" not in out


def test_search_history_没有对话要说明(store, llm):
    tools, _ = _tools(store, llm, conversation_id=None)
    out = tools["search_history"].invoke({"keyword": "x"})
    assert "没有" in out or "不在" in out


# ---------- search_assets ----------


def test_search_assets_按名字找素材(store, llm):
    project = _project(store)
    store.add_asset(
        project.id, kind="video", name="巷口追逐白模", source="blockout",
        url="/api/assets/file/x", content=b"abc",
    )
    tools, _ = _tools(store, llm)
    out = tools["search_assets"].invoke({"project_id": project.id, "query": "白模"})
    assert "白模" in out
    assert "asset_" in out, "条目要带 id"


def test_search_assets_没有匹配要说清楚(store, llm):
    project = _project(store)
    tools, _ = _tools(store, llm)
    out = tools["search_assets"].invoke({"project_id": project.id, "query": "不存在的东西"})
    assert "没有匹配" in out


# ---------- save_asset ----------


def _script():
    from app.domain import Script

    return Script(
        title="排钱", logline="排钱", themes=["体面"],
        characters=[{"id": "char_ke", "name": "孔乙己"}],
        locations=[{"id": "loc_01", "name": "咸亨酒店"}],
        scenes=[{
            "id": "scene_001", "title": "排钱", "chapter_refs": ["ch_001"],
            "location_id": "loc_01", "characters": ["char_ke"],
            "purpose": "排钱", "conflict": "体面与铜钱",
        }],
        source={"chapter_count": 1, "chapter_ids": ["ch_001"]},
    )


def test_save_asset_把图片存成角色定妆照(store, llm):
    project = _project(store)
    store.create_version(project, _script(), source_type="generation", label="初稿")
    asset = store.add_asset(
        project.id, kind="image", name="孔乙己定妆", source="upload",
        url="/api/assets/file/img1", content=b"png",
    )
    tools, _ = _tools(store, llm)
    out = tools["save_asset"].invoke({
        "project_id": project.id, "entity": "孔乙己", "asset_id": asset.id,
    })
    assert "定妆照" in out
    style = store.get_video_style(project.id)
    assert style["reference_images"].get("char_ke") == "/api/assets/file/img1"


def test_save_asset_不认识的名字要拒(store, llm):
    project = _project(store)
    store.create_version(project, _script(), source_type="generation", label="初稿")
    asset = store.add_asset(
        project.id, kind="image", name="图", source="upload",
        url="/api/assets/file/i", content=b"png",
    )
    tools, _ = _tools(store, llm)
    out = tools["save_asset"].invoke({
        "project_id": project.id, "entity": "查无此人", "asset_id": asset.id,
    })
    assert "没有" in out


def test_save_asset_只收图片(store, llm):
    project = _project(store)
    store.create_version(project, _script(), source_type="generation", label="初稿")
    asset = store.add_asset(
        project.id, kind="video", name="一段视频", source="upload",
        url="/api/assets/file/v", content=b"mp4",
    )
    tools, _ = _tools(store, llm)
    out = tools["save_asset"].invoke({
        "project_id": project.id, "entity": "孔乙己", "asset_id": asset.id,
    })
    assert "图片" in out


# ---------- attach_asset ----------


def test_attach_asset_挂到镜头并留版本(store, llm):
    project = _project(store)
    _version(store, project.id, [_approved_shot()])
    asset = store.add_asset(
        project.id, kind="video", name="白模", source="blockout",
        url="/api/assets/file/b", content=b"mp4",
    )
    tools, _ = _tools(store, llm)
    out = tools["attach_asset"].invoke({
        "project_id": project.id, "asset_id": asset.id, "shot_ids": "shot_a_000",
    })
    assert "挂到" in out
    versions = store.list_video_versions(project.id)
    latest = versions[0]
    shot = next(s for s in latest.shots if s["id"] == "shot_a_000")
    assert asset.id in shot["reference_asset_ids"]


def test_attach_asset_别作品的素材要拒(store, llm):
    project = _project(store)
    _version(store, project.id, [_approved_shot()])
    other = _project(store, title="别的")
    asset = store.add_asset(
        other.id, kind="video", name="他片白模", source="blockout",
        url="/api/assets/file/x", content=b"mp4",
    )
    tools, _ = _tools(store, llm)
    out = tools["attach_asset"].invoke({
        "project_id": project.id, "asset_id": asset.id,
    })
    assert "不属于本作品" in out


# ---------- generate_video ----------


def test_generate_video_缺风格比例要先问(store, llm, monkeypatch):
    """缺项不替用户定——这三样直接决定成片长什么样。"""
    import app.deps as app_deps

    monkeypatch.setattr(app_deps, "store", lambda: store)
    project = _project(store)
    _version(store, project.id, [_approved_shot()])
    tools, _ = _tools(store, llm)

    out = tools["generate_video"].invoke({
        "project_id": project.id, "shot_id": "shot_a_000",
    })
    assert "视觉风格" in out
    assert "画面比例" in out


def test_generate_video_未批准要拒(store, llm, monkeypatch):
    import app.deps as app_deps

    monkeypatch.setattr(app_deps, "store", lambda: store)
    project = _project(store)
    shot = _approved_shot()
    shot["prompt_status"] = "needs_review"
    _version(store, project.id, [shot])
    tools, _ = _tools(store, llm)

    out = tools["generate_video"].invoke({
        "project_id": project.id, "shot_id": "shot_a_000",
        "style": "realistic", "ratio": "16:9",
    })
    assert "批准" in out


def test_generate_video_参数齐了才提交(store, llm, monkeypatch):
    import app.deps as app_deps
    import app.video.service as video_service

    monkeypatch.setattr(app_deps, "store", lambda: store)
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)
    project = _project(store)
    _version(store, project.id, [_approved_shot()])
    tools, _ = _tools(store, llm)

    out = tools["generate_video"].invoke({
        "project_id": project.id, "shot_id": "shot_a_000",
        "style": "realistic", "ratio": "16:9",
    })
    assert "已提交" in out
    jobs = store.list_video_jobs(project.id)
    assert len(jobs) == 1
    assert jobs[0].params.get("aspect_ratio") == "16:9"


def test_generate_video_组镜头投组头发组提示词(store, llm, monkeypatch):
    import app.deps as app_deps
    import app.video.service as video_service

    monkeypatch.setattr(app_deps, "store", lambda: store)
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)
    project = _project(store)
    _version(store, project.id, [
        _approved_shot("shot_a_000", group_id="g1", group_prompt="组提示词全文"),
        {
            "id": "shot_a_001", "scene_id": "scene_001", "order": 1, "duration_sec": 3,
            "video_prompt": "P1", "group_id": "g1", "group_index": 1,
            "prompt_status": "needs_review",
        },
    ])
    tools, _ = _tools(store, llm)

    out = tools["generate_video"].invoke({
        "project_id": project.id, "shot_id": "shot_a_001",
        "style": "realistic", "ratio": "16:9",
    })
    assert "已提交" in out
    jobs = store.list_video_jobs(project.id)
    assert len(jobs) == 1
    assert jobs[0].shot_id == "shot_a_000", "组镜头要投组头"
    assert jobs[0].prompt == "组提示词全文"


# ---------- submit_videos 带参考素材 ----------


def test_submit_videos_可挂参考素材(store, llm, monkeypatch):
    import app.deps as app_deps
    import app.video.service as video_service

    monkeypatch.setattr(app_deps, "store", lambda: store)
    monkeypatch.setattr(video_service, "_submit_video_job", lambda job: None)
    project = _project(store)
    _version(store, project.id, [_approved_shot()])
    asset = store.add_asset(
        project.id, kind="video", name="白模", source="blockout",
        url="/api/assets/file/b", content=b"mp4",
    )
    tools, _ = _tools(store, llm)

    out = tools["submit_videos"].invoke({
        "project_id": project.id, "shot_ids": "shot_a_000",
        "reference_asset_ids": asset.id,
    })
    assert "已提交" in out
    jobs = store.list_video_jobs(project.id)
    assert len(jobs) == 1
    assert asset.id in (jobs[0].params.get("reference_asset_ids") or [])


def test_submit_videos_别作品的素材要拒(store, llm):
    project = _project(store)
    _version(store, project.id, [_approved_shot()])
    other = _project(store, title="别的")
    asset = store.add_asset(
        other.id, kind="video", name="他片白模", source="blockout",
        url="/api/assets/file/x", content=b"mp4",
    )
    tools, _ = _tools(store, llm)
    out = tools["submit_videos"].invoke({
        "project_id": project.id, "reference_asset_ids": asset.id,
    })
    assert "不属于本作品" in out


@pytest.mark.parametrize("kind", ["video", "audio", "image"])
def test_list_assets_按类型过滤(store, llm, kind):
    project = _project(store)
    store.add_asset(
        project.id, kind=kind, name="一条", source="upload",
        url="/api/assets/file/a", content=b"x",
    )
    tools, _ = _tools(store, llm)
    out = tools["list_assets"].invoke({"project_id": project.id, "kind": kind})
    assert "一条" in out
