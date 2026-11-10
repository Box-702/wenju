# =====================================================================
# client.py —— 模型调用统一门面
#
# 全项目只有这一个类会去构造具体模型客户端。上层业务（图节点 / 剧组 /
# 对话 / 质检 / 文生图）统一通过它取能力，不关心厂商差异：
#
#   llm.available         是否配置了对话模型（无 key 时上层走本地回退）
#   llm.chat()            可 bind_tools 的聊天模型（ReAct / 工具调用）
#   llm.structured(Schema) 结构化输出（json_mode，兼容推理模型）
#   llm.system_prompt()   带语言约束的统一系统提示词
#   llm.vision()          视觉客户端（定妆图质检）
#   llm.image()           文生图客户端（参考资产）
#
# 没有 key 时返回 None / 抛异常由调用方兜底，保证「导入 → 生成 → 提议
# → 审阅 → 接受」链路在离线状态下依然可跑通、可演示。
# =====================================================================

from __future__ import annotations

from typing import Any, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

from ..config import Settings
from .base import Modality, ModelConfig
from .image import ImageClient, build_image_client
from .providers import build_chat_model, get_provider_spec
from .registry import describe_providers, resolve_chat, resolve_image, resolve_vision
from .vision import VisionClient

T = TypeVar("T", bound=BaseModel)


class _JsonPromptStructured:
    """不支持 json_object 的厂商用的结构化输出降级实现。

    语义与 ``with_structured_output(schema, method="json_mode")`` 等价：
    接受消息、返回 ``schema`` 实例。差别只在于——
      1. 用提示词要求模型只输出 JSON（而不是请求参数 response_format）；
      2. 自己解析 JSON（容忍 markdown 围栏、前后缀文字）；
      3. 解析失败会带上错误信息重采样一次。

    用在 MiniMax 这类「标准路径可用但不支持 response_format」的厂商上。
    """

    def __init__(self, model: BaseChatModel, schema: type[T]) -> None:
        self._model = model
        self._schema = schema

    def _json_hint(self) -> str:
        try:
            fields = ", ".join(self._schema.model_fields.keys())
        except Exception:  # noqa: BLE001
            fields = ""
        return (
            "只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码围栏。\n"
            f"字段：{fields}\n"
            "所有值都要符合下面的 JSON Schema：\n"
            f"{self._schema.model_json_schema()}"
        )

    @staticmethod
    def _parse(text: str) -> dict[str, Any]:
        import json
        import re

        raw = (text or "").strip()
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, flags=re.DOTALL)
        if m:
            raw = m.group(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            for i, ch in enumerate(raw):
                if ch in "{[":
                    try:
                        return json.loads(raw[i:])
                    except json.JSONDecodeError:
                        continue
            raise ValueError(f"无法解析 JSON：{raw[:200]}") from None

    def invoke(self, messages: Any, **kwargs: Any) -> T:
        hint = self._json_hint()
        prepared = _append_json_hint(messages, hint)
        last_error: Exception | None = None
        for _attempt in range(2):
            resp = self._model.invoke(prepared, **kwargs)
            text = str(getattr(resp, "content", "") or "")
            try:
                return self._schema.model_validate(self._parse(text))
            except Exception as e:  # noqa: BLE001
                last_error = e
                prepared = _append_json_hint(
                    messages,
                    hint + f"\n\n（上一次输出无法解析成 JSON：{e}。请只输出合法 JSON。）",
                )
        raise RuntimeError(f"结构化输出失败：{last_error}") from last_error


def _append_json_hint(messages: Any, hint: str) -> list[Any]:
    """把 JSON 要求追加到消息列表末尾（不改原消息对象）。"""
    from langchain_core.messages import HumanMessage

    if isinstance(messages, str):
        return [HumanMessage(content=f"{messages}\n\n{hint}")]
    if isinstance(messages, dict):
        items = [messages]
    else:
        items = list(messages)
    items.append(HumanMessage(content=hint))
    return items


class LLM:
    """对话模型 + 视觉 + 文生图的统一入口（单例，见 app/deps.py）。"""

    def __init__(
        self,
        settings: Settings,
        store: Any = None,
        *,
        temperature: float | None = None,
    ) -> None:
        self.settings = settings
        self._store = store
        self.config: ModelConfig | None = resolve_chat(settings, store)
        # temperature 覆盖：仅供需要可复现输出的场合（如提示词质量 eval）。
        # 生产链路保持默认温度，不要在这里改。
        if self.config is not None and temperature is not None:
            self.config.temperature = temperature
        self._model: BaseChatModel | None = (
            build_chat_model(self.config) if self.config else None
        )

    # ---------- 对话能力 ----------

    @property
    def available(self) -> bool:
        """是否配置并实例化了对话模型。"""
        return self._model is not None

    @property
    def provider_label(self) -> str:
        """厂商展示名；无模型时为 local-fallback（前端与提示词都用它）。"""
        return self.config.label if self.config else "local-fallback"

    def chat(self) -> BaseChatModel:
        """返回可 bind_tools 的聊天模型（用于 ReAct 工具调用循环）。"""
        if self._model is None:
            raise RuntimeError(
                "模型未配置。请在 .env 中设置 OPENAI_API_KEY / ZHIPUAI_API_KEY / "
                "DEEPSEEK_API_KEY，或在前端「设置 → 模型」里添加 provider。"
            )
        return self._model

    def chat_multimodal(self) -> BaseChatModel:
        """返回可识别图片的聊天模型（本轮用户消息带图时用）。

        OpenAI 兼容端点对 image_url 内容块是透传的，所以视觉厂商（GLM /
        GPT / Qwen / Ollama 多模态）直接用当前对话模型即可；唯一要换型号的
        是 DeepSeek——它的对话模型不看图，需要切到同厂的视觉型号
        （ProviderSpec.vision_model）。构造出的模型仍可 bind_tools。
        """
        base = self.chat()  # 未配置时统一报错
        config = self.config
        if not config:
            return base
        spec = get_provider_spec(config.provider)
        if spec and spec.vision_model and config.provider == "deepseek" \
                and "vision" not in (config.model or "").lower():
            import dataclasses

            vision_config = dataclasses.replace(config, model=spec.vision_model)
            return build_chat_model(vision_config)
        return base

    def structured(self, schema: type[T]) -> Any:
        """返回一个接受消息、输出 ``schema`` 实例的结构化可运行对象。

        默认用 ``json_mode``（response_format=json_object）：
        对 OpenAI 兼容服务最稳，且 DeepSeek 的思考模式不支持强制
        tool_choice，不能走 function_calling。

        **降级路径**：MiniMax 的 /chat/completions 不支持 json_object
        （实测报 2013 unknown response_format type）。这类厂商在
        ProviderSpec 里声明 ``supports_json_mode=False``，此处自动降级为
        「提示词要求只输出 JSON + 容错解析 + 失败重采样」——
        语义等价，只多一次重试的机会。
        """
        if self._model is None:
            raise RuntimeError("模型未配置，无法进行结构化输出。")
        if self._supports_json_mode():
            return self._model.with_structured_output(schema, method="json_mode")
        return _JsonPromptStructured(self._model, schema)

    def _supports_json_mode(self) -> bool:
        """当前厂商是否支持 response_format=json_object。"""
        from .providers import get_provider_spec

        if not self.config:
            return True
        spec = get_provider_spec(self.config.provider)
        # 未登记的厂商（自建网关）默认按支持处理，保持旧行为。
        return True if spec is None else spec.supports_json_mode

    def system_prompt(self) -> str:
        """构造统一的系统提示词，包括语言约束与输出规范。"""
        lang = self.settings.output_language or "zh-CN"
        if lang.lower().startswith("zh"):
            style = "简体中文" if lang.lower() in {"zh-cn", "zh-hans", "zh"} else "繁體中文"
            lang_note = (
                f"所有面向用户的自然语言字段必须使用 {style}（{lang}），包括 title、logline、"
                "purpose、conflict、action、dialogue、line、emotion、subtext、"
                "entry_state、exit_state、adaptation_notes.reason。"
                "id 字段必须保持纯 ASCII（小写字母、数字、下划线）。"
            )
        else:
            lang_note = (
                f"所有面向用户的自然语言字段必须使用 {lang}。"
                "id 字段必须保持纯 ASCII（小写字母、数字、下划线）。"
            )
        return (
            "你是剧本改写助手，负责把用户的小说或剧本片段改编成结构化剧本。"
            "你只输出符合给定 JSON Schema 的有效对象，不要输出额外说明或 Markdown 代码围栏。"
            f"{lang_note}"
        )

    # ---------- 其他能力维度 ----------

    def vision(self) -> VisionClient:
        """视觉客户端（看图质检）。未配置时 available=False，调用方应跳过。"""
        config = resolve_vision(self.settings, self._store)
        return VisionClient(config) if config else VisionClient(
            ModelConfig(provider="none", model="", label="未配置视觉模型")
        )

    def image(self) -> ImageClient | None:
        """文生图客户端。未配置时返回 None。"""
        return build_image_client(resolve_image(self.settings, self._store))

    # ---------- 自省 ----------

    def describe(self) -> dict[str, Any]:
        """当前模型配置摘要（不含 key），供 /api/status 与设置页展示。"""
        config = self.config
        return {
            "available": self.available,
            "provider": config.provider if config else "local-fallback",
            "provider_label": self.provider_label,
            "model": config.model if config else "",
            "base_url": config.base_url if config else "",
            "source": config.source if config else "none",
            "vision": self.describe_vision(),
            "image": self.describe_image(),
            "providers": describe_providers(),
        }

    def describe_vision(self) -> dict[str, Any]:
        config = resolve_vision(self.settings, self._store)
        return {
            "available": bool(config and config.api_key),
            "provider": config.provider if config else "",
            "model": config.model if config else "",
        }

    def describe_image(self) -> dict[str, Any]:
        """当前生效的文生图配置（设置页「生图模型」展示用）。"""
        config = resolve_image(self.settings, self._store)
        return {
            "available": bool(config and config.api_key),
            "provider": config.provider if config else "",
            "label": config.label if config else "",
            "model": config.model if config else "",
            "source": config.source if config else "none",
        }


def build_llm(settings: Settings | None = None, store: Any = None, *, temperature: float | None = None) -> LLM:
    """构造模型门面。未显式传入时使用全局 Settings。

    temperature 覆盖仅供需要可复现输出的场合（如提示词质量 eval 固定用 0）。
    """
    from ..config import get_settings

    return LLM(settings or get_settings(), store, temperature=temperature)


# 供上层按能力维度做校验/展示。
__all__ = ["LLM", "Modality", "ModelConfig", "build_llm", "get_provider_spec"]
