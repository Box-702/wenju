# =====================================================================
# test_provider_catalog.py —— 内置厂商目录与探测（D-42）
#
# 锁住三件事：
#   1. /providers/specs 返回内置目录（含用户点名的 xiaomi / anthropic / doubao），
#      且路由没被 /providers/{provider_id} 吃掉（"specs" 会被当 id → 404）；
#   2. /providers/probe 不建行也能拿 key 问厂商（一步流的核心）；
#   3. ProviderSpec.chat_models 目录字段随 describe_providers 透出。
# =====================================================================

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
import app.api.video as video_api
from app.agent.tasks import SubAgentRunner
from app.main import create_app


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    monkeypatch.setattr(api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store))
    return TestClient(create_app())


def test_specs_catalog_lists_builtin_vendors(client):
    """目录含用户点名的厂商；每项带 label/base_url/chat_models/docs。"""
    specs = client.get("/api/providers/specs").json()
    names = [s["name"] for s in specs]
    for want in ("deepseek", "zhipu", "qwen", "moonshot", "xiaomi", "doubao", "openai", "anthropic"):
        assert want in names, f"目录缺厂商 {want}"
    ds = next(s for s in specs if s["name"] == "deepseek")
    assert ds["base_url"] and ds["label"]
    assert isinstance(ds["chat_models"], list) and ds["chat_models"]
    assert ds["docs"].startswith("http")


def test_specs_route_not_shadowed_by_provider_id(client):
    """/providers/specs 必须命中目录而不是被当成 provider id（404）。"""
    resp = client.get("/api/providers/specs")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_probe_reports_without_creating_row(client, store):
    """probe 不建行：拿假 base_url + key 也能得到结构化失败说明。"""
    before = len(store.list_api_providers(kind="llm"))
    resp = client.post(
        "/api/providers/probe",
        json={"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-fake"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body.get("error")
    assert len(store.list_api_providers(kind="llm")) == before, "probe 不该建厂商行"


def test_probe_requires_key_and_url(client):
    """缺 key / 缺 url 直接 422（min_length 校验），不打网络。"""
    assert client.post("/api/providers/probe", json={"base_url": "x", "api_key": ""}).status_code == 422


def test_probe_returns_models_on_success(client, monkeypatch):
    """probe 复用 _probe_openai_compatible：厂商返回清单时透传 models。"""
    monkeypatch.setattr(
        video_api, "_probe_openai_compatible",
        lambda base, key, model="": (True, "连接成功，厂商返回 2 个模型", ["m-a", "m-b"]),
    )
    resp = client.post(
        "/api/providers/probe",
        json={"base_url": "https://api.deepseek.com", "api_key": "sk-real"},
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["models"] == ["m-a", "m-b"]


def test_describe_providers_exposes_chat_models():
    """LLM 厂商能力清单带目录字段（/api/status 的 providers 一节）。"""
    from app.llm.registry import describe_providers

    ds = next(p for p in describe_providers() if p["name"] == "deepseek")
    assert "chat_models" in ds and ds["chat_models"]
    assert "docs" in ds
