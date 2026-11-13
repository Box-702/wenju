# =====================================================================
# test_assets.py —— 素材接口 + 模型选择优先级（本次新增能力）
#
# 覆盖三件事：
#   1. 素材上传/登记/回放/删除的全链路（含越权格式与超大文件防御）；
#   2. 删除 provider 时同步清除引用它的模型偏好；
#   3. resolve_chat 的优先级：UI 显式偏好 > CHAT_PROVIDER > env 链。
# =====================================================================

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
from app.agent.tasks import SubAgentRunner
from app.config import Settings
from app.main import create_app
from app.store import Store


# ---------- 素材接口 ----------


def _client(store: Store, settings: Settings, monkeypatch) -> TestClient:
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: None)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    monkeypatch.setattr(api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store))
    return TestClient(create_app())


def _project(client: TestClient) -> str:
    resp = client.post("/api/projects/import", data={"raw_text": "雨夜。凌晨三点的问剧，霓虹灯还亮着，键盘声停不下来，改稿的人还没走。", "title": "素材测试"})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_asset_upload_list_fetch_delete(store, settings, monkeypatch, tmp_path):
    settings.workspace_root = str(tmp_path)  # 上传落盘指到临时目录
    client = _client(store, settings, monkeypatch)
    pid = _project(client)

    # 上传
    resp = client.post(
        f"/api/projects/{pid}/assets/upload",
        files={"file": ("white_model.mp4", io.BytesIO(b"\x00" * 128), "video/mp4")},
    )
    assert resp.status_code == 200, resp.text
    asset = resp.json()
    assert asset["source"] == "upload"
    assert asset["url"].startswith("/api/assets/file/")

    # 列表
    lst = client.get(f"/api/projects/{pid}/assets").json()
    assert [a["id"] for a in lst] == [asset["id"]]

    # 回放
    fetched = client.get(asset["url"])
    assert fetched.status_code == 200
    assert fetched.headers["content-type"].startswith("video/")

    # 删除后列表为空、文件不可再取
    assert client.delete(f"/api/assets/{asset['id']}").status_code == 200
    assert client.get(f"/api/projects/{pid}/assets").json() == []
    assert client.get(asset["url"]).status_code == 404


def test_asset_upload_rejects_bad_ext(store, settings, monkeypatch, tmp_path):
    settings.workspace_root = str(tmp_path)
    client = _client(store, settings, monkeypatch)
    pid = _project(client)
    resp = client.post(
        f"/api/projects/{pid}/assets/upload",
        files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_asset_url_endpoint_is_gone(store, settings, monkeypatch, tmp_path):
    """公网直链那条路整条拆掉了：素材只能上传进来，不能再登记外部链接。

    理由见 app/media/inline.py 的注释——服务商只认 http(s) 或 data:base64，
    而本机回放地址它是拉不到的，所以「登记一条公网直链」这个概念没有存在必要。
    """
    settings.workspace_root = str(tmp_path)
    client = _client(store, settings, monkeypatch)
    pid = _project(client)
    assert client.post(f"/api/projects/{pid}/assets/url", json={"url": "https://cdn.example.com/a.mp4"}).status_code == 404


def test_asset_upload_records_media_properties(store, settings, monkeypatch, tmp_path):
    """入库时量出时长/尺寸：用户选参考素材时就能当场看见这段片子能不能用。"""
    import subprocess

    from app.video.qa import _ffmpeg

    ffmpeg = _ffmpeg()
    if not ffmpeg:
        pytest.skip("本机无 ffmpeg，跳过媒体属性测量测试")

    clip_path = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=10:duration=3",
         "-pix_fmt", "yuv420p", str(clip_path)],
        check=True, capture_output=True,
    )

    settings.workspace_root = str(tmp_path)
    client = _client(store, settings, monkeypatch)
    pid = _project(client)
    resp = client.post(
        f"/api/projects/{pid}/assets/upload",
        files={"file": ("clip.mp4", io.BytesIO(clip_path.read_bytes()), "video/mp4")},
    )
    assert resp.status_code == 200, resp.text
    asset = resp.json()
    assert asset["source"] == "upload"
    assert asset["duration_sec"] is not None
    assert 2.0 <= asset["duration_sec"] <= 4.0


# ---------- 删 provider 清偏好 ----------


def test_delete_provider_clears_preference(store):
    p = store.create_api_provider(
        kind="llm", name="deepseek", label="DeepSeek",
        base_url="https://api.deepseek.com/v1", api_key="sk-test", config={"model": "deepseek-flash"},
    )
    store.set_model_preference(task_type="chat", provider_id=p.id, model_name="deepseek-flash")
    assert store.get_model_preference("chat") is not None

    assert store.delete_api_provider(p.id) is True
    pref = store.get_model_preference("chat")
    # 偏好记录被清除（或 provider_id 置空），不会再解析到已删除的 provider
    assert pref is None or pref.provider_id is None


# ---------- resolve_chat 优先级：UI 偏好 > CHAT_PROVIDER > env 链 ----------


def _settings(**overrides: str) -> Settings:
    base = {
        "DATABASE_URL": "sqlite:///./data/_unused.db",
        "CHECKPOINTER": "memory",
        "OPENAI_API_KEY": "",
        "ZHIPUAI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "CHECKPOINT_DSN": "",
        "CHAT_PROVIDER": "",
        "VISION_PROVIDER": "",
        "VISION_API_KEY": "",
        "VISION_MODEL": "",
        "IMAGE_PROVIDER": "",
        "IMAGE_API_KEY": "",
    }
    base.update(overrides)
    return Settings(**base)


def test_resolve_chat_ui_preference_beats_chat_provider(store):
    p = store.create_api_provider(
        kind="llm", name="zhipu", label="智谱 UI 选择",
        base_url="https://open.bigmodel.cn/api/paas/v4/", api_key="zk-test", config={"model": "GLM-5.3-Flash"},
    )
    store.set_model_preference(task_type="chat", provider_id=p.id, model_name="GLM-5.3-Flash")

    from app.llm import resolve_chat

    # CHAT_PROVIDER 指向 deepseek，但 UI 显式偏好（智谱）必须赢
    cfg = resolve_chat(_settings(CHAT_PROVIDER="deepseek", DEEPSEEK_API_KEY="ds"), store)
    assert cfg is not None
    assert cfg.provider == "zhipu"
    assert cfg.model == "GLM-5.3-Flash"
    assert cfg.source == "db"


def test_resolve_chat_without_db_is_unconfigured(store):
    """D-48：清掉 UI 偏好且 DB 无行 = 未配置（env 兜底链已整拆）。"""
    from app.llm import resolve_chat

    cfg = resolve_chat(_settings(DEEPSEEK_API_KEY="ds", CHAT_PROVIDER="deepseek"), store)
    assert cfg is None
