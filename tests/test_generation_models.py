"""Per-task model choices only expose configured, enabled models."""

from types import SimpleNamespace

import pytest

from app.chat.tools.generation_models import options, resolve
from app.chat.tools import build_chat_tools
from app.chat.conductor import _handle_choice_confirm


class Store:
    def __init__(self):
        self.rows = [
            SimpleNamespace(id="img", kind="image", name="zhipu", label="智谱",
                            enabled=True, api_key="key", config={"models": [
                                {"model": "image-fast", "enabled": True},
                                {"model": "image-old", "enabled": False}]}),
            SimpleNamespace(id="off", kind="image", name="other", label="停用",
                            enabled=False, api_key="key", config={"model": "image-off"}),
        ]

    def list_api_providers(self, kind):
        return [row for row in self.rows if row.kind == kind]

    def get_api_provider(self, provider_id):
        return next((row for row in self.rows if row.id == provider_id), None)


def test_generation_model_choice_is_validated_against_live_configuration():
    store = Store()
    assert options(store, "image") == [{"provider_id": "img", "provider": "zhipu",
                                         "label": "智谱", "model": "image-fast"}]
    assert resolve(store, "image", "img", "image-fast")[1] == "image-fast"
    with pytest.raises(ValueError, match="未配置或已停用"):
        resolve(store, "image", "off", "image-off")


def test_image_model_list_issues_real_choice_card_and_consumes_once(store, llm):
    a = store.create_api_provider(kind="image", name="minimax", label="MiniMax",
                                  api_key="test", config={"model": "MiniMax-H3-Image"})
    store.create_api_provider(kind="image", name="seedance", label="Seedream",
                              api_key="test", config={"model": "seedream-test"})
    collector = {"project_id": None, "conversation_id": "conv_test", "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    tools["create_project"].invoke({"title": "雨夜", "raw_text": "他在旧车站等待。" * 20})
    pid = collector["project_id"]

    out = tools["list_generation_models"].invoke({"kind": "image"})
    card = collector["payloads"][-1]
    assert "本轮到此为止" in out
    assert card["type"] == "option_confirm"
    assert store.has_pending_model_choice(pid, "conv_test", "image")
    assert len(card["questions"][0]["options"]) == 2
    assert all("provider_id" not in o["label"] for o in card["questions"][0]["options"])
    selected = next(o for o in card["questions"][0]["options"] if o["provider_id"] == a.id)
    reply = _handle_choice_confirm(store, conversation_id="conv_test", meta={
        "intent": "confirm_options", "choice_id": card["choice_id"],
        "choices": [{"question": "这次生成使用哪个模型？", "option_ids": [selected["id"]]}],
    })
    assert "已确认" in reply["reply"]
    assert not store.has_pending_model_choice(pid, "conv_test", "image")
    assert store.take_confirmed_model_choice(pid, "conv_test", "image") == (a.id, "MiniMax-H3-Image")
    assert store.take_confirmed_model_choice(pid, "conv_test", "image") is None
