# =====================================================================
# registry.py —— 配置解析（.env / DB → ModelConfig）
#
# 上层业务永远不直接读环境变量或数据库：只问这里要一个 ModelConfig。
# 两条配置来源，优先级明确：
#   1. 显式指定（CHAT_PROVIDER / VISION_PROVIDER / IMAGE_PROVIDER）；
#   2. .env 的厂商优先级链（保持历史行为：OPENAI > ZHIPUAI > DEEPSEEK）；
#   3. 数据库 api_providers / model_preferences（前端「设置」里配的）。
#
# 每一维（chat / vision / image）各自解析，互不干扰：对话用 DeepSeek、
# 质检用智谱视觉、文生图用 CogView 这种混搭是允许的。
# =====================================================================

from __future__ import annotations

import logging
import os
from typing import Any

from ..keys import minimax_keys, ring_for
from .base import ModelConfig
from .providers import all_provider_specs, get_provider_spec

log = logging.getLogger(__name__)

# 兼容别名：.env 里可能写成 zhipuai / glm / openai-compatible 等。
_ALIASES = {
    "openai-compatible": "openai",
    "zhipuai": "zhipu",
    "glm": "zhipu",
    "bigmodel": "zhipu",
    "dashscope": "qwen",
    "kimi": "moonshot",
    "local": "ollama",
}

# .env 优先级链（历史行为，不要随意调整顺序）。
_ENV_CHAIN = ("openai", "zhipu", "deepseek")


def _canonical(name: str) -> str:
    key = (name or "").strip().lower()
    return _ALIASES.get(key, key)


def _settings_attr(settings: Any, *names: str) -> str:
    """按顺序取第一个非空的 Settings 字段。"""
    for name in names:
        value = getattr(settings, name, "") or ""
        if str(value).strip():
            return str(value).strip()
    return ""


def _from_settings(provider: str, settings: Any) -> ModelConfig | None:
    """从 .env / Settings 构造某个厂商的 ModelConfig；没有 key 则返回 None。"""
    name = _canonical(provider)
    spec = get_provider_spec(name)
    if spec is None:
        return None

    api_key = base_url = model = ""
    extra_body: dict[str, Any] = {}

    if name == "openai":
        api_key = _settings_attr(settings, "openai_api_key")
        base_url = _settings_attr(settings, "openai_base_url")
        model = _settings_attr(settings, "openai_model")
    elif name == "zhipu":
        api_key = _settings_attr(settings, "zhipuai_api_key")
        base_url = _settings_attr(settings, "zhipuai_base_url")
        model = _settings_attr(settings, "zhipuai_model")
    elif name == "deepseek":
        api_key = _settings_attr(settings, "deepseek_api_key")
        base_url = _settings_attr(settings, "deepseek_base_url")
        model = _settings_attr(settings, "deepseek_model")
        # 推理模型：由 .env 控制是否开启思考链（默认关闭，直出 content）。
        extra_body = {"_thinking": bool(getattr(settings, "deepseek_thinking", False))}
    elif name == "minimax":
        # 一把 key 打通对话 / 视频（H3）/ 配音（T2A）。
        # 多把 key 并存时取「当前最该用」的那把（见 app/keys.py）。
        # 注意：对话链路不像视频/配音那样能中途换 key 重试，这里只是开局选优。
        # 优先用 Settings 里的完整列表（它自己读 .env，不依赖 load_dotenv 是否跑过）。
        keys = list(getattr(settings, "minimax_key_list", None) or []) or minimax_keys(
            _settings_attr(settings, "minimax_api_key")
        )
        api_key = ring_for("minimax", keys).pick()
        base_url = _settings_attr(settings, "minimax_base_url")
        model = _settings_attr(settings, "minimax_model")
    elif name == "ollama":
        # 本地服务无需 key；端点与模型可由环境变量覆盖。
        api_key = _settings_attr(settings, "ollama_api_key") or "not-needed"
        base_url = _settings_attr(settings, "ollama_base_url")
        model = _settings_attr(settings, "ollama_model")
    else:
        # 未在 Settings 里声明字段的厂商（moonshot / qwen / 自建网关）：
        # 支持用真实环境变量配置（.env 需被 python-dotenv 加载进 os.environ 才生效）。
        api_key = os.environ.get(spec.env_key, "").strip()

    if spec.requires_key and not api_key:
        return None

    return ModelConfig(
        provider=name,
        label=spec.label,
        model=model or spec.default_model,
        api_key=api_key,
        base_url=base_url or spec.default_base_url,
        extra_body=extra_body,
        source="env",
    )


def _from_store(store: Any, kind: str, task_type: str = "") -> ModelConfig | None:
    """从数据库的 api_providers / model_preferences 构造 ModelConfig。"""
    if store is None:
        return None
    try:
        # 先看用户为某类任务显式选择的默认模型。
        if task_type:
            pref = store.get_model_preference(task_type)
            if pref and pref.provider_id:
                row = store.get_api_provider(pref.provider_id)
                if row and row.api_key:
                    return _from_db_row(row, override_model=pref.model_name,
                                        params=getattr(pref, "params", None) or {})
        for row in store.list_api_providers(kind=kind):
            if row.enabled and row.api_key:
                return _from_db_row(row)
    except Exception as e:  # noqa: BLE001
        log.debug("读取 DB 模型配置失败：%s", e)
    return None


def provider_models(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """一个厂商下列出的模型清单。

    新形态：config["models"] = [{"name": "GLM-5.3", "model": "glm-5.3", "enabled": true}, ...]
    老形态：只有 config["model"] —— 视作一条清单，行为不变。
    """
    cfg = config or {}
    items = cfg.get("models")
    if isinstance(items, list):
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            model = str(it.get("model") or "").strip()
            if not model:
                continue
            out.append({
                "name": str(it.get("name") or model),
                "model": model,
                "enabled": bool(it.get("enabled", True)),
            })
        if out:
            return out
    single = str(cfg.get("model") or "").strip()
    return [{"name": single, "model": single, "enabled": True}] if single else []


def active_model(config: dict[str, Any] | None) -> str:
    """厂商当前生效的模型：清单里第一个启用的；没有清单就用老的单 model。"""
    for it in provider_models(config):
        if it.get("enabled"):
            return str(it.get("model") or "")
    return ""


def _from_db_row(
    row: Any, override_model: str = "", params: dict[str, Any] | None = None
) -> ModelConfig:
    config = getattr(row, "config", None) or {}
    spec = get_provider_spec(_canonical(getattr(row, "name", "")))
    return ModelConfig(
        provider=_canonical(getattr(row, "name", "")) or "custom",
        label=getattr(row, "label", "") or (spec.label if spec else ""),
        model=override_model or active_model(config) or config.get("model") or (spec.default_model if spec else ""),
        api_key=getattr(row, "api_key", "") or "",
        base_url=getattr(row, "base_url", "") or (spec.default_base_url if spec else ""),
        extra_body=dict(params or {}),
        source="db",
    )


# ---------- 三个能力维度的解析入口 ----------


def _explicit_chat_pref(store: Any) -> ModelConfig | None:
    """「设置 → 模型」里用户显式设为默认的对话模型（task_type=chat 的偏好）。"""
    if store is None:
        return None
    try:
        pref = store.get_model_preference("chat")
        if pref and pref.provider_id:
            row = store.get_api_provider(pref.provider_id)
            if row and row.enabled and row.api_key:
                return _from_db_row(row, override_model=pref.model_name,
                                    params=getattr(pref, "params", None) or {})
    except Exception as e:  # noqa: BLE001
        log.debug("读取对话模型偏好失败：%s", e)
    return None


def resolve_chat(settings: Any, store: Any = None) -> ModelConfig | None:
    """对话模型：**DB 权威**（UI 显式偏好 > DB 厂商行），env 只在 DB 空时兜底。

    BYOK 的应有之义（D-41）：用户在「设置 → 模型」里选的必须说了算，
    env 不再能悄悄盖过它。env 链退为"空库兜底"——新装/无 DB 时仍能靠 .env 跑起来，
    一旦 DB 有配置就以 DB 为准。
    """
    pref = _explicit_chat_pref(store) or _from_store(store, "llm", "chat")
    if pref:
        return pref
    # DB 空：回落 env（保持"克隆下来就能跑"，也保持无 store 的既有行为）
    forced = _canonical(_settings_attr(settings, "chat_provider"))
    if forced:
        return _from_settings(forced, settings)
    for name in _ENV_CHAIN:
        config = _from_settings(name, settings)
        if config:
            return config
    return None


def resolve_vision(settings: Any, store: Any = None) -> ModelConfig | None:
    """视觉模型：**DB 权威**，env 只在 DB 空时兜底（同 resolve_chat，D-41）。

    DB 兜底顺序：vision 行 → llm 行的 vision 偏好 → 任意 vision 行 → 任意 llm 行。
    env 兜底顺序：VISION_API_KEY 显式 → VISION_PROVIDER 强制 → 支持视觉的厂商链。
    """
    from_store = _from_store(store, "vision") or _from_store(store, "llm", "vision")
    if from_store:
        return from_store

    forced = _canonical(_settings_attr(settings, "vision_provider"))
    explicit_key = _settings_attr(settings, "vision_api_key")

    if explicit_key:
        base = _from_settings(forced, settings) if forced else None
        spec = get_provider_spec(forced) if forced else None
        return ModelConfig(
            provider=forced or "custom",
            label=spec.label if spec else "自定义视觉",
            model=_settings_attr(settings, "vision_model") or (spec.vision_model if spec else ""),
            api_key=explicit_key,
            base_url=_settings_attr(settings, "vision_base_url") or (base.base_url if base else ""),
            source="env",
        )

    if forced:
        spec = get_provider_spec(forced)
        config = _from_settings(forced, settings)
        if config and spec and spec.supports_vision:
            chosen = _settings_attr(settings, "vision_model")
            config.model = chosen or spec.vision_model or config.model
            return config

    # 复用当前对话厂商（若它自带视觉能力），否则按 key 的可用性挑一家。
    for name in ("deepseek", "zhipu", "openai", "qwen"):
        spec = get_provider_spec(name)
        if spec is None or not spec.supports_vision:
            continue
        config = _from_settings(name, settings)
        if config:
            chosen = _settings_attr(settings, "vision_model")
            config.model = chosen or spec.vision_model or config.model
            return config
    return None


def resolve_image(settings: Any, store: Any = None) -> ModelConfig | None:
    """文生图模型：**DB 权威**（image_gen 偏好 > image 行），env 只在 DB 空时兜底。

    DB 行没填 key 时回落 .env 的同厂商 key（CogView 与智谱 GLM 共用
    ZHIPUAI_API_KEY）——设置页只让用户「选模型」不重复粘 key；
    迁移（D-41）会把 key 种进 DB，之后这条回落基本不再触发。
    """
    # 用户在设置里选过生图模型：模型/端点以用户选择为准，key 缺失时回落 .env。
    if store is not None:
        try:
            pref = store.get_model_preference("image_gen")
            if pref and pref.provider_id:
                row = store.get_api_provider(pref.provider_id)
                if row:
                    name = _canonical(row.name) or "cogview"
                    spec = get_provider_spec(name)
                    source = _from_settings("zhipu" if name in ("cogview", "zhipu") else name, settings)
                    api_key = (row.api_key or "") or (source.api_key if source else "")
                    model = pref.model_name or active_model(row.config) or (row.config.get("model") or "")
                    model = model or (spec.image_model if spec else "")
                    if model and api_key:
                        return ModelConfig(
                            provider=name,
                            label=row.label or (spec.label if spec else name),
                            model=model,
                            api_key=api_key,
                            base_url=row.base_url or (spec.default_base_url if spec else ""),
                            source="db",
                        )
        except Exception as e:  # noqa: BLE001
            log.debug("读取生图模型偏好失败：%s", e)

    from_store = _from_store(store, "image", "image_gen")
    if from_store:
        return from_store

    forced = _canonical(_settings_attr(settings, "image_provider"))
    explicit_key = _settings_attr(settings, "image_api_key")

    if forced or explicit_key:
        name = forced or "cogview"
        spec = get_provider_spec(name)
        # cogview 与 zhipu 共用同一个 key。
        source = _from_settings("zhipu" if name == "cogview" else name, settings)
        api_key = explicit_key or (source.api_key if source else "")
        if api_key:
            return ModelConfig(
                provider=name,
                label=spec.label if spec else "自定义文生图",
                model=_settings_attr(settings, "image_model") or (spec.image_model if spec else ""),
                api_key=api_key,
                base_url=_settings_attr(settings, "image_base_url")
                or (spec.default_base_url if spec else ""),
                source="env",
            )

    for name in ("cogview", "openai"):
        spec = get_provider_spec(name)
        if spec is None:
            continue
        source = _from_settings("zhipu" if name == "cogview" else name, settings)
        if source and source.api_key:
            return ModelConfig(
                provider=name,
                label=spec.label,
                model=spec.image_model,
                api_key=source.api_key,
                base_url=spec.default_base_url,
                source="env",
            )
    return None


def describe_providers() -> list[dict[str, Any]]:
    """给设置页/接口用的厂商能力清单（不含任何 key）。"""
    return [
        {
            "name": s.name,
            "label": s.label,
            "default_base_url": s.default_base_url,
            "default_model": s.default_model,
            "env_key": s.env_key,
            "requires_key": s.requires_key,
            "modalities": [
                m
                for m, ok in (
                    ("chat", True),
                    ("vision", s.supports_vision),
                    ("image", s.supports_image),
                )
                if ok
            ],
        }
        for s in all_provider_specs()
    ]


def resolve_tts(settings: Any, store: Any = None) -> ModelConfig | None:
    """TTS 模型：**DB 权威**（tts 行），env 只在 DB 空时兜底（同 resolve_chat，D-41）。

    minimax 一把 key 打通对话/视频/配音；env 兜底时与视频 Provider 共用一个
    key 环（坏 key 在这里也跳过，见 app/keys.py）。
    """
    from_store = _from_store(store, "tts", "tts")
    if from_store:
        return from_store

    provider = _canonical(_settings_attr(settings, "tts_provider")) or "minimax"
    api_key = _settings_attr(settings, "tts_api_key")
    base_url = _settings_attr(settings, "tts_base_url")
    if not api_key and provider == "minimax":
        # 与视频 Provider 共用一个 key 环：坏掉的 key 在这里也跳过。
        keys = list(getattr(settings, "minimax_key_list", None) or []) or minimax_keys()
        api_key = ring_for("minimax", keys).pick()
    if not api_key:
        return None
    return ModelConfig(
        provider=provider,
        label="MiniMax 语音合成" if provider == "minimax" else provider,
        model=_settings_attr(settings, "tts_model") or "speech-02-hd",
        api_key=api_key,
        base_url=base_url or ("https://api.minimax.cn" if provider == "minimax" else ""),
        source="env",
    )
