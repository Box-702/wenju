# =====================================================================
# test_reference_material.py —— 参考视频从「参考素材」里来的那条路
#
# 覆盖三件事：
#   1. 本地素材在提交给服务商前被内联成 data URI（服务商拉不到本机回放地址）；
#   2. 镜头存的是素材 id，提交时与「自动接力的前镜成片」合并，最多 3 条；
#   3. 素材不合格（时长超出服务商窗口 / 不存在 / 不是视频）当场拒绝，
#      而不是提交后白跑一次生成。
#
# 不调网络：所有断言都在「提交前」的边界上。
# =====================================================================

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
from app.agent.tasks import SubAgentRunner
from app.main import create_app
from app.media.inline import inline_asset_url, inline_media_urls


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    """和 test_api.py 同一套接法：路由统一走 app.api.deps，替换一处即可覆盖全部路由。"""
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    monkeypatch.setattr(
        api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store)
    )
    return TestClient(create_app())


class _FakeAsset:
    def __init__(self, name, kind, content, url=None):
        self.id = "asset_x"
        self.name = name
        self.kind = kind
        self.content = content
        self.url = url or "/api/assets/file/asset_x"
        self.project_id = "proj_x"


class _FakeStore:
    def __init__(self, assets):
        self._assets = assets

    def get_asset(self, asset_id):
        return self._assets.get(asset_id)


# ---------- 内联：本地 → data URI，公网 → 原样 ----------


def test_local_asset_is_inlined_as_data_uri():
    """服务商只认 http(s):// 或 data:...;base64；本机回放地址它拉不到。"""
    store = _FakeStore({"a1": _FakeAsset("白模.mp4", "video", b"\x00\x01\x02")})
    out = inline_asset_url(store, "/api/assets/file/a1")
    assert out.startswith("data:video/mp4;base64,")
    assert base64.b64decode(out.split(",", 1)[1]) == b"\x00\x01\x02"


def test_public_url_passes_through():
    """公网直链（成片直链、上一镜接力）服务商自己能拉，原样透传。"""
    store = _FakeStore({})
    assert inline_asset_url(store, "https://cdn.example.com/a.mp4") == "https://cdn.example.com/a.mp4"


def test_user_uploaded_portrait_is_inlined_too():
    """用户自己上传的定妆照也是本地文件——同样要内联。

    这条路径以前是坏的：资产库把上传的图片记成 /api/assets/file/xxx 相对地址，
    直接塞给服务商会被判 invalid param（定妆照一挂上就出不了片）。
    """
    store = _FakeStore({"a1": _FakeAsset("定妆照.png", "image", b"\x89PNG\r\n\x1a\n")})
    out = inline_asset_url(store, "/api/assets/file/a1")
    assert out.startswith("data:image/png;base64,")


def test_data_uri_passes_through():
    """图片库里已经是 data URI 的定妆图，原样透传，不要二次编码。"""
    store = _FakeStore({})
    assert inline_asset_url(store, "data:image/png;base64,AAA") == "data:image/png;base64,AAA"


def test_missing_asset_is_skipped_not_raised():
    """素材被删了 → 跳过这一条，不让整批出片失败。"""
    store = _FakeStore({})
    assert inline_asset_url(store, "/api/assets/file/gone") is None
    assert inline_media_urls(store, ["/api/assets/file/gone", "https://x/y.mp4"]) == ["https://x/y.mp4"]


def test_oversized_asset_is_skipped():
    """超过内联上限的素材跳过：base64 会膨胀 1/3，硬塞进去只会让请求爆掉。"""
    from app.media import inline as inline_mod

    big = _FakeAsset("big.mp4", "video", b"\x00" * (inline_mod.MAX_INLINE_BYTES + 1))
    store = _FakeStore({"big": big})
    assert inline_asset_url(store, "/api/assets/file/big") is None


# ---------- 镜头侧：素材 id + 自动接力合并，最多 3 条 ----------


def _video_version(store, project_id, shots):
    return store.create_video_version(
        project_id=project_id, shots=shots, label="测试",
    )


def test_manual_assets_merge_with_auto_chain_under_cap(store, client, sample_text, monkeypatch):
    """用户挑的参考素材 + 自动接力的前镜成片，合并后最多 3 条。"""
    import app.api.video as video_api

    project = client.post(
        "/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"},
    ).json()
    pid = project["id"]

    asset = store.add_asset(
        pid, kind="video", name="白模.mp4", source="upload",
        id="asset_m", url="/api/assets/file/asset_m", content=b"\x00" * 10,
    )
    version = _video_version(store, pid, [
        {
            "id": "shot_prev",
            "scene_id": "scene_001",
            "order": 0,
            "video_prompt": "A wide shot.",
            "prompt_status": "approved",
        },
        {
            "id": "shot_scene_001_000",
            "scene_id": "scene_001",
            "order": 1,
            "video_prompt": "A close-up.",
            "prompt_status": "approved",
            "chain_from": 0,
            "reference_asset_ids": [asset.id],
        },
    ])

    # 上一镜已经出片（自动接力会拿它的公网直链当参考）
    prev = store.create_video_job(
        project_id=pid, shot_id="shot_prev", provider="minimax", model="default",
        prompt="x", params={},
    )
    store.update_video_job(prev.id, status="succeeded", video_url="https://cdn.example.com/prev.mp4")

    monkeypatch.setattr(video_api, "_submit_video_job", lambda job: None)
    resp = client.post(
        f"/api/projects/{pid}/video/generate",
        json={
            "shot_id": "shot_scene_001_000",
            "provider": "minimax",
            "model": "default",
            "prompt": "A close-up.",
            "version_id": version.id,
        },
    )
    assert resp.status_code == 200, resp.text
    job = store.get_video_job(resp.json()["id"])

    # 落库的 params 里存的是素材 id 对应的回放地址；真正喂给服务商前才内联。
    stored = job.params.get("reference_videos") or []
    assert "/api/assets/file/asset_m" in stored
    assert len(stored) <= 3

    params = video_api._job_params(dict(job.params or {}))
    refs = params.reference_videos
    assert len(refs) <= 3
    assert any(r.startswith("data:video/mp4;base64,") for r in refs)
    # 自动接力的公网直链原样透传
    assert "https://cdn.example.com/prev.mp4" in refs


def test_reference_asset_must_belong_to_project(store, client, sample_text):
    """别人的素材 id 不能拿来当参考——403/404，不静默接受。"""
    project = client.post(
        "/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"},
    ).json()
    pid = project["id"]
    other = store.add_asset(
        "proj_other", kind="video", name="别人的.mp4", source="upload",
        url="/api/assets/file/asset_other", content=b"\x00" * 10,
    )
    version = _video_version(store, pid, [
        {
            "id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
            "video_prompt": "A close-up.", "prompt_status": "approved",
        },
    ])
    resp = client.put(
        f"/api/projects/{pid}/video-versions/{version.id}/shots/shot_scene_001_000/prompt",
        json={
            "prompt": "A close-up.",
            "decision": "save",
            "reference_asset_ids": [other.id],
        },
    )
    assert resp.status_code == 404, resp.text


def test_reference_asset_must_be_video(store, client, sample_text):
    """图片不能当参考视频挂上去。"""
    project = client.post(
        "/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"},
    ).json()
    pid = project["id"]
    image = store.add_asset(
        pid, kind="image", name="定妆照.png", source="upload",
        url="/api/assets/file/asset_img", content=b"\x00" * 10,
    )
    version = _video_version(store, pid, [
        {
            "id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
            "video_prompt": "A close-up.", "prompt_status": "approved",
        },
    ])
    resp = client.put(
        f"/api/projects/{pid}/video-versions/{version.id}/shots/shot_scene_001_000/prompt",
        json={
            "prompt": "A close-up.",
            "decision": "save",
            "reference_asset_ids": [image.id],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "不是视频" in resp.text


def test_reference_asset_duration_out_of_window_is_rejected(store, client, sample_text, monkeypatch):
    """素材时长超出服务商窗口 → 当场拒绝，别等提交后白跑一次生成。"""
    import app.media.probe as probe_mod

    project = client.post(
        "/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"},
    ).json()
    pid = project["id"]
    clip = store.add_asset(
        pid, kind="video", name="长片.mp4", source="upload",
        url="/api/assets/file/asset_long", content=b"\x00" * 10,
    )
    monkeypatch.setattr(probe_mod, "duration_seconds", lambda data, name: 42.0)
    version = _video_version(store, pid, [
        {
            "id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
            "video_prompt": "A close-up.", "prompt_status": "approved",
        },
    ])
    resp = client.put(
        f"/api/projects/{pid}/video-versions/{version.id}/shots/shot_scene_001_000/prompt",
        json={
            "prompt": "A close-up.",
            "decision": "save",
            "reference_asset_ids": [clip.id],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "秒" in resp.text


def test_reference_asset_low_fps_is_rejected(store, client, sample_text, monkeypatch):
    """低帧率素材（如 15fps 录屏）也当场拒绝。

    2026-11-10 完整跑时撞出来的：15fps 的参考片任务建得好，生成阶段才报
    `video fps 15.000, expected [23.976, 60]`——白等一轮才发现。
    """
    import app.media.probe as probe_mod

    project = client.post(
        "/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"},
    ).json()
    pid = project["id"]
    clip = store.add_asset(
        pid, kind="video", name="录屏.mp4", source="upload",
        url="/api/assets/file/asset_lows", content=b"\x00" * 10,
    )
    monkeypatch.setattr(probe_mod, "duration_seconds", lambda data, name: 3.0)
    monkeypatch.setattr(probe_mod, "video_fps", lambda data, name: 15.0)
    version = _video_version(store, pid, [
        {
            "id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
            "video_prompt": "A close-up.", "prompt_status": "approved",
        },
    ])
    resp = client.put(
        f"/api/projects/{pid}/video-versions/{version.id}/shots/shot_scene_001_000/prompt",
        json={
            "prompt": "A close-up.",
            "decision": "save",
            "reference_asset_ids": [clip.id],
        },
    )
    assert resp.status_code == 400, resp.text
    assert "帧率" in resp.text


@pytest.mark.parametrize("seconds,ok", [(5.0, True), (14.0, True), (1.0, False), (30.0, False)])
def test_reference_window_bounds(store, client, sample_text, monkeypatch, seconds, ok):
    """2–15 秒是服务商的硬约束（实测 expected [2000, 15000] ms）。"""
    import app.media.probe as probe_mod

    project = client.post(
        "/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"},
    ).json()
    pid = project["id"]
    clip = store.add_asset(
        pid, kind="video", name="clip.mp4", source="upload",
        url="/api/assets/file/asset_c", content=b"\x00" * 10,
    )
    monkeypatch.setattr(probe_mod, "duration_seconds", lambda data, name: seconds)
    version = _video_version(store, pid, [
        {
            "id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
            "video_prompt": "A close-up.", "prompt_status": "approved",
        },
    ])
    resp = client.put(
        f"/api/projects/{pid}/video-versions/{version.id}/shots/shot_scene_001_000/prompt",
        json={
            "prompt": "A close-up.",
            "decision": "save",
            "reference_asset_ids": [clip.id],
        },
    )
    assert resp.status_code == (200 if ok else 400), resp.text
