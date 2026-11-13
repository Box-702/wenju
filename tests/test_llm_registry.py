# =====================================================================
# test_llm_registry.py —— 模型接入层的配置解析
#
# 只测「配置 → ModelConfig」这一步（纯逻辑，不触网、不构造真实模型）。
# D-48 定案：模型配置只走 DB（BYOK），env 链彻底移除——本文件的用例
# 全部改为「建 DB 行 → 解析」，并锁住「env 里有 key 也不生效」。
# =====================================================================

from __future__ import annotations

from app.config import Settings
from app.llm import describe_providers, resolve_chat, resolve_image, resolve_vision
from app.llm.providers import build_chat_model, get_provider_spec


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


def test_no_keys_means_no_chat_config():
    cfg = resolve_chat(_settings())
    assert cfg is None


def test_env_keys_alone_no_longer_configure_anything(store):
    """D-48：.env 里的 key 不再参与模型选择——空库就是未配置。"""
    cfg = resolve_chat(_settings(OPENAI_API_KEY="oa", DEEPSEEK_API_KEY="ds"), store)
    assert cfg is None


def test_db_row_order_decides_vendor(store):
    """无显式偏好时取第一个启用的行（list_api_providers 按创建时间倒序，
    最新配的优先——D-41 以来的既有行为，这里锁住它）。"""
    store.create_api_provider(kind="llm", name="openai", label="OpenAI 兼容",
                              api_key="oa", config={"model": "gpt-4o-mini"})
    store.create_api_provider(kind="llm", name="deepseek", label="DeepSeek",
                              api_key="ds", config={"model": "deepseek-chat"})
    cfg = resolve_chat(_settings(), store)
    assert cfg is not None and cfg.provider == "deepseek" and cfg.source == "db"


def test_deepseek_thinking_becomes_extra_body(store):
    """DEEPSEEK_THINKING 是行为开关（env 豁免之一），对 DB 解析出的
    deepseek 配置仍然生效——它不选模型也不选 key。"""
    store.create_api_provider(kind="llm", name="deepseek", label="DeepSeek",
                              api_key="ds", config={"model": "deepseek-chat"})
    cfg = resolve_chat(_settings(DEEPSEEK_THINKING="false"), store)
    assert cfg is not None
    assert cfg.provider == "deepseek"
    assert cfg.extra_body == {"_thinking": False}

    model = build_chat_model(cfg)
    # 关闭思考链必须落到请求体上，否则推理模型会把内容写进 reasoning_content。
    assert model.extra_body == {"thinking": {"type": "disabled"}}


def test_ui_preference_forces_vendor(store):
    """界面显式偏好压过行顺序（CHAT_PROVIDER 环境变量已退役）。"""
    store.create_api_provider(kind="llm", name="openai", label="OpenAI 兼容",
                              api_key="oa", config={"model": "gpt-4o-mini"})
    p = store.create_api_provider(kind="llm", name="deepseek", label="DeepSeek",
                                  api_key="ds", config={"model": "deepseek-chat"})
    store.set_model_preference(task_type="chat", provider_id=p.id, model_name="deepseek-chat")
    cfg = resolve_chat(_settings(CHAT_PROVIDER="openai"), store)
    assert cfg is not None and cfg.provider == "deepseek"


def test_vision_falls_back_to_chat_vendor(store):
    """没有 vision 行时复用对话厂商（若带视觉能力），换成视觉模型名。"""
    store.create_api_provider(kind="llm", name="deepseek", label="DeepSeek",
                              api_key="ds", config={"model": "deepseek-chat"})
    cfg = resolve_vision(_settings(), store)
    assert cfg is not None
    assert cfg.provider == "deepseek"
    assert cfg.model == "deepseek-v4-flash-vision-exp"


def test_vision_row_beats_chat_reuse(store):
    """vision 行优先于「复用对话厂商」路径。"""
    store.create_api_provider(kind="llm", name="deepseek", label="DeepSeek",
                              api_key="ds", config={"model": "deepseek-chat"})
    store.create_api_provider(kind="vision", name="zhipu", label="智谱",
                              api_key="zk", config={"model": "glm-4v-flash"})
    cfg = resolve_vision(_settings(), store)
    assert cfg is not None and cfg.provider == "zhipu"
    assert cfg.model == "glm-4v-flash"


def test_image_resolves_from_db_row(store):
    store.create_api_provider(kind="image", name="cogview", label="智谱",
                              api_key="zp", config={"model": "cogview-4-250304"})
    cfg = resolve_image(_settings(), store)
    assert cfg is not None and cfg.provider == "cogview"
    assert cfg.model == "cogview-4-250304"


def test_image_returns_none_without_db_row(store):
    """env 里有 zhipu/deepseek key 也不再自动配生图——BYOK 界面没建行就是未配置。"""
    assert resolve_image(_settings(ZHIPUAI_API_KEY="zp", DEEPSEEK_API_KEY="ds"), store) is None


def test_provider_table_exposes_modalities():
    providers = {p["name"]: p for p in describe_providers()}
    assert "chat" in providers["deepseek"]["modalities"]
    assert "vision" in providers["deepseek"]["modalities"]
    assert "image" in providers["zhipu"]["modalities"]
    # 本地模型不需要 key
    assert get_provider_spec("ollama").requires_key is False


def test_minimax_chat_row_resolves_with_factory_defaults(store):
    """MiniMax 对话行：模型/端点缺省走厂商表（key 由用户在界面贴）。"""
    store.create_api_provider(kind="llm", name="minimax", label="MiniMax",
                              api_key="mm", config={})
    cfg = resolve_chat(_settings(), store)
    assert cfg is not None and cfg.provider == "minimax"
    assert cfg.model == "MiniMax-Text-01"
    assert cfg.base_url.startswith("https://api.minimax.cn")


def test_minimax_declares_no_json_mode():
    """MiniMax 的 /chat/completions 不支持 response_format=json_object。

    实测报 2013 unknown response_format type——所以结构化输出必须走降级路径。
    """
    spec = get_provider_spec("minimax")
    assert spec is not None
    assert spec.supports_json_mode is False
    # 其他厂商不受影响
    assert get_provider_spec("deepseek").supports_json_mode is True
    assert get_provider_spec("zhipu").supports_json_mode is True


def test_structured_falls_back_when_provider_has_no_json_mode():
    """不支持 json_mode 的厂商，structured() 要降级成「提示词 + 容错解析」。"""
    from pydantic import BaseModel

    from app.llm.client import LLM

    class Tiny(BaseModel):
        title: str

    from app.deps import store as _noop  # noqa: F401 —— 仅示意依赖方向
    llm = LLM(_settings())
    llm.config = type("C", (), {"provider": "minimax", "model": "MiniMax-Text-01",
                                "api_key": "mm", "base_url": "https://api.minimax.cn/v1",
                                "extra_body": {}, "temperature": None})()
    assert llm.config is not None
    calls: list[list] = []

    class FakeModel:
        def invoke(self, messages, **kwargs):
            calls.append(messages)
            return type("R", (), {"content": '{"title": "夜街"}'})()

    llm._model = FakeModel()  # type: ignore[assignment]
    out = llm.structured(Tiny).invoke([{"role": "user", "content": "给个标题"}])
    assert isinstance(out, Tiny)
    assert out.title == "夜街"
    # 降级路径必须把 JSON 要求追加到消息里
    assert calls, "降级路径应该真的调了一次模型"

    def _text(m) -> str:
        return str(m.get("content", "")) if isinstance(m, dict) else str(getattr(m, "content", ""))

    assert any("JSON" in _text(m) for m in calls[-1])


def test_structured_uses_json_mode_for_normal_providers():
    """普通厂商仍走 json_mode（行为不变）。"""
    from app.llm.client import LLM

    llm = LLM(_settings())
    llm.config = type("C", (), {"provider": "deepseek", "model": "deepseek-chat",
                                "api_key": "ds", "base_url": "https://api.deepseek.com",
                                "extra_body": {}, "temperature": None})()
    assert llm._supports_json_mode() is True
