# =====================================================================
# providers.py —— 各模型厂商适配器
#
# 适配方式：全部走 OpenAI 兼容协议（langchain-openai 的 ChatOpenAI +
# 原生 httpx 的 /chat/completions、/images/generations），差异只体现在
# 「默认端点 / 默认模型 / 额外请求体」三处，用 ProviderSpec 声明。
#
# 之所以不引入各家官方 SDK：OpenAI 兼容已是国内厂商事实标准，
# 一套协议覆盖 OpenAI / DeepSeek / 智谱 GLM / Moonshot / Qwen / vLLM /
# Ollama / 任何自建网关，维护成本最低、离线可回退。
#
# 新增厂商：在 _register_builtins() 里加一条 ProviderSpec（或运行期调用
# register_provider()），上层无需改动。非 OpenAI 兼容的厂商（如 Anthropic
# Messages API）需要另写一个真正实现该协议的子类，不属于本文件的范畴。
# =====================================================================

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .base import ModelConfig, ProviderSpec

_PROVIDERS: dict[str, ProviderSpec] = {}


def register_provider(spec: ProviderSpec) -> None:
    """注册（或覆盖）一个厂商声明。"""
    _PROVIDERS[spec.name] = spec


def get_provider_spec(name: str) -> ProviderSpec | None:
    return _PROVIDERS.get(name.strip().lower())


def all_provider_specs() -> list[ProviderSpec]:
    return list(_PROVIDERS.values())


def _deepseek_extra_body(config: ModelConfig) -> dict[str, Any]:
    """DeepSeek 推理模型：关闭 thinking 才会把内容写进 content。

    thinking=disabled 直出 content；开启时内容进 reasoning_content 且慢约 2.2 倍。
    具体开关由 ModelConfig.extra_body 传入（见 registry）。
    """
    thinking = bool(config.extra_body.get("_thinking", False))
    return {"thinking": {"type": "enabled" if thinking else "disabled"}}


def build_chat_model(config: ModelConfig) -> BaseChatModel:
    """按 ModelConfig 构造可 bind_tools 的聊天模型。"""
    spec = get_provider_spec(config.provider)
    kwargs: dict[str, Any] = {
        "model": config.model,
        # 本地服务（Ollama / vLLM）通常不校验 key，给占位值避免 SDK 报缺少鉴权。
        "api_key": config.api_key or "not-needed",
        "temperature": config.temperature,
        # 必须开流式：对话 conductor 用 langgraph messages 模式把增量推给前端，
        # 不开的话回复（含思考内容）在整轮结束时一次性到达，前端没有流式观感。
        "streaming": True,
    }
    base_url = (config.base_url or (spec.default_base_url if spec else "")).rstrip("/")
    if base_url:
        kwargs["base_url"] = base_url

    extra = dict(config.extra_body)
    extra.pop("_thinking", None)
    if spec:
        extra = {**spec.extra_body_for(config), **extra}
    if extra:
        kwargs["extra_body"] = extra
    return ChatOpenAI(**kwargs)


def _register_builtins() -> None:
    register_provider(ProviderSpec(
        name="openai",
        label="OpenAI 兼容",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        env_key="OPENAI_API_KEY",
        docs="https://platform.openai.com/api-keys",
        supports_vision=True,
        vision_model="gpt-4o-mini",
        supports_image=True,
        image_model="gpt-image-1",
        chat_models=("gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3-mini"),
    ))
    register_provider(ProviderSpec(
        name="deepseek",
        label="DeepSeek",
        default_base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        env_key="DEEPSEEK_API_KEY",
        docs="https://platform.deepseek.com/api_keys",
        supports_vision=True,
        vision_model="deepseek-v4-flash-vision-exp",
        extra_body=_deepseek_extra_body,
        chat_models=("deepseek-chat", "deepseek-reasoner"),
    ))
    register_provider(ProviderSpec(
        name="zhipu",
        label="智谱 GLM",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-flash",
        env_key="ZHIPUAI_API_KEY",
        docs="https://open.bigmodel.cn/usercenter/apikeys",
        supports_vision=True,
        vision_model="glm-4v-flash",
        # 同一个 key 同时可调 GLM / CogView（文生图）/ CogVideoX（视频）。
        supports_image=True,
        image_model="cogview-3-flash",
        chat_models=("glm-4.5", "glm-4.5-air", "glm-4-plus", "glm-4-flash"),
    ))
    register_provider(ProviderSpec(
        name="moonshot",
        label="Moonshot Kimi",
        default_base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-8k",
        env_key="MOONSHOT_API_KEY",
        docs="https://platform.moonshot.cn/console/api-keys",
        chat_models=("kimi-k2-0905-preview", "kimi-thinking-preview",
                     "moonshot-v1-8k", "moonshot-v1-32k"),
    ))
    register_provider(ProviderSpec(
        name="qwen",
        label="通义千问",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        env_key="DASHSCOPE_API_KEY",
        docs="https://bailian.console.aliyun.com/",
        supports_vision=True,
        vision_model="qwen-vl-plus",
        chat_models=("qwen-max", "qwen-plus", "qwen-turbo", "qwq-plus"),
    ))
    register_provider(ProviderSpec(
        name="ollama",
        label="Ollama 本地",
        default_base_url="http://127.0.0.1:11434/v1",
        default_model="qwen2.5:7b",
        env_key="OLLAMA_API_KEY",
        requires_key=False,
        supports_vision=True,
        vision_model="llava",
    ))
    # 文生图专用厂商：与 "zhipu" 区分开，便于 IMAGE_PROVIDER=cogview 直接选中。
    register_provider(ProviderSpec(
        name="cogview",
        label="智谱 CogView",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        env_key="ZHIPUAI_API_KEY",
        docs="https://open.bigmodel.cn/usercenter/apikeys",
        supports_image=True,
        image_model="cogview-3-flash",
    ))
    register_provider(ProviderSpec(
        name="minimax",
        label="MiniMax",
        default_base_url="https://api.minimax.cn/v1",
        default_model="MiniMax-Text-01",
        env_key="MINIMAX_API_KEY",
        docs="https://platform.minimaxi.com/",
        # 同一个 key 同时可调 MiniMax-Text（对话）/ H3（视频）/ T2A（配音）——
        # 一把 key 打通全链路的选项。
        #
        # ⚠️ MiniMax 的 /chat/completions **不支持 response_format=json_object**
        # （实测报 2013 unknown response_format type），所以结构化输出必须走
        # 「提示词 + 容错解析」这条路，见 client.LLM.structured()。
        supports_json_mode=False,
        chat_models=("MiniMax-Text-01", "abab6.5s-chat"),
    ))
    # ---- 2026-11-11 新增内置厂商（用户点名：小米 / Anthropic / 豆包）----
    # /models 与 /chat/completions 路由均已实测存在（401=拒鉴权而非 404）。
    register_provider(ProviderSpec(
        name="xiaomi",
        label="小米 MiMo",
        default_base_url="https://api.xiaomimimo.com/v1",
        default_model="mimo-v2-flash",
        env_key="XIAOMI_API_KEY",
        docs="https://platform.xiaomimimo.com/",
        chat_models=("mimo-v2-pro", "mimo-v2-flash"),
    ))
    register_provider(ProviderSpec(
        name="anthropic",
        label="Anthropic Claude",
        # 官方已提供 OpenAI 兼容入口（/v1/chat/completions，实测路由存在、
        # 鉴权层先挡）；个别高级参数仍以 Messages 协议为准。中转网关也能接。
        default_base_url="https://api.anthropic.com/v1",
        default_model="claude-sonnet-4-5",
        env_key="ANTHROPIC_API_KEY",
        docs="https://console.anthropic.com/settings/keys",
        supports_vision=True,
        vision_model="claude-sonnet-4-5",
        chat_models=("claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5"),
    ))
    register_provider(ProviderSpec(
        name="doubao",
        label="豆包（火山方舟）",
        # 方舟的 OpenAI 兼容 base 带 /api/v3；model 参数要填「模型 ID」
        # （endpoint 形如 ep-xxxx），发现清单里给的就是它。
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-seed-1-6-flash-250715",
        env_key="ARK_API_KEY",
        docs="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        supports_vision=True,
        vision_model="doubao-seed-1-6-vision",
        chat_models=("doubao-seed-1-6-250615", "doubao-seed-1-6-flash-250715",
                     "doubao-1-5-pro-32k-250115"),
    ))


_register_builtins()
