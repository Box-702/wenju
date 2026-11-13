# =====================================================================
# registry.py —— 模型配置解析（DB/BYOK 唯一权威 → ModelConfig）
#
# 上层业务永远不直接读环境变量或数据库：只问这里要一个 ModelConfig。
# 配置权威**只有数据库**（api_providers / model_preferences，即「设置 → 模型」
# 里 BYOK 配的那份）。.env 从 D-48 起彻底退出模型选择：不再有
# CHAT_PROVIDER / *_API_KEY / *_MODEL 的回落链，空库就是「未配置」，
# 界面引导用户去设置里贴 key——key 和选择都是用户的，界面必须说话算数。
#
# 两个例外（不是配置入口，是运行时机制）：
#   - app/keys.py 的备用 key 轮换仍读 MINIMAX_API_KEY_FALLBACKS——那是欠费/
#     失效时的故障转移安全网，主 key 永远以 DB 为准；
#   - DEEPSEEK_THINKING 是行为开关（思考链开关），不选模型也不选 key。
#
# 厂商与模型名严格分离（2026-11-13 用户指正）：label 只写厂商
# （MiniMax / 智谱 / 火山方舟…），模型名（MiniMax-H3-Image、cogview-4…）
# 只出现在厂商的模型清单里。
#
# 每一维（chat / vision / image / tts）各自解析，互不干扰：对话用 DeepSeek、
# 质检用智谱视觉、文生图用别家这种混搭是允许的。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

from .base import ModelConfig
from .providers import all_provider_specs, get_provider_spec

log = logging.getLogger(__name__)

# 兼容别名：DB 行/历史数据里可能写成 zhipuai / glm / openai-compatible 等。
_ALIASES = {
    "openai-compatible": "openai",
    "zhipuai": "zhipu",
    "glm": "zhipu",
    "bigmodel": "zhipu",
    "dashscope": "qwen",
    "kimi": "moonshot",
    "local": "ollama",
}


def _canonical(name: str) -> str:
    key = (name or "").strip().lower()
    return _ALIASES.get(key, key)


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


def _from_store(store: Any, kind: str, task_type: str = "") -> ModelConfig | None:
    """从数据库的 api_providers / model_preferences 构造 ModelConfig（唯一权威）。"""
    if store is None:
        return None
    try:
        # 先看用户为某类任务显式选择的默认模型。
        if task_type:
            pref = store.get_model_preference(task_type)
            if pref and pref.provider_id:
                row = store.get_api_provider(pref.provider_id)
                if row and row.enabled and row.api_key:
                    return _from_db_row(row, override_model=pref.model_name,
                                        params=getattr(pref, "params", None) or {})
        for row in store.list_api_providers(kind=kind):
            if row.enabled and row.api_key:
                return _from_db_row(row)
    except Exception as e:  # noqa: BLE001
        log.debug("读取 DB 模型配置失败：%s", e)
    return None


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


def _apply_thinking(cfg: ModelConfig | None, settings: Any) -> ModelConfig | None:
    """deepseek 思考链开关（DEEPSEEK_THINKING）：行为开关不是模型选择，
    是 env 仅存的两个豁免之一（另一个是备用 key 轮换安全网）。"""
    if cfg is not None and cfg.provider == "deepseek":
        thinking = bool(getattr(settings, "deepseek_thinking", False))
        cfg.extra_body = {**(cfg.extra_body or {}), "_thinking": thinking}
    return cfg


# ---------- 四个能力维度的解析入口（DB/BYOK 唯一权威，D-48） ----------


def resolve_chat(settings: Any, store: Any = None) -> ModelConfig | None:
    """对话模型：UI 显式偏好 > DB 第一个启用带 key 的 llm 行 > 未配置（None）。

    没有 env 兜底：空库返回 None，上层走演示模式并在界面引导贴 key。
    """
    return _apply_thinking(
        _explicit_chat_pref(store) or _from_store(store, "llm", "chat"), settings)


def resolve_vision(settings: Any, store: Any = None) -> ModelConfig | None:
    """视觉模型：vision 行 > llm 行上的显式 vision 偏好 > 复用对话厂商（若带视觉能力）。

    复用对话厂商时换成该厂商的视觉模型名——质检看图不该用纯文本模型。
    注意不能拿 `_from_store(store, "llm", ...)` 的「任意行兜底」当这一层：
    它会把对话行原样端来（模型名还是 chat 的），跳过换名。
    """
    cfg = _from_store(store, "vision")
    if cfg:
        return cfg
    # 用户在界面上给 vision 任务显式选过某家（可能是 llm 行）：尊重所选模型名。
    if store is not None:
        try:
            pref = store.get_model_preference("vision")
            if pref and pref.provider_id:
                row = store.get_api_provider(pref.provider_id)
                if row and row.enabled and row.api_key:
                    return _from_db_row(row, override_model=pref.model_name,
                                        params=getattr(pref, "params", None) or {})
        except Exception as e:  # noqa: BLE001
            log.debug("读取 vision 偏好失败：%s", e)
    chat = resolve_chat(settings, store)
    if chat is not None:
        spec = get_provider_spec(chat.provider)
        if spec and spec.supports_vision and spec.vision_model:
            chat.model = spec.vision_model
        return chat
    return None


def resolve_image(settings: Any, store: Any = None) -> ModelConfig | None:
    """文生图模型：image_gen 偏好 > image 行 > 未配置（None）。"""
    return _from_store(store, "image", "image_gen")


def resolve_tts(settings: Any, store: Any = None) -> ModelConfig | None:
    """配音模型：tts 偏好 > tts 行 > 未配置（None）。

    MiniMax 一把 key 打通对话/视频/配音是 DB 里三行共用同一个 key 的结果，
    不再是 env 的隐式联动。
    """
    return _from_store(store, "tts", "tts")


def describe_providers() -> list[dict[str, Any]]:
    """给设置页/接口用的厂商能力清单（不含任何 key）。"""
    return [
        {
            "name": s.name,
            "label": s.label,
            "default_base_url": s.default_base_url,
            "default_model": s.default_model,
            "requires_key": s.requires_key,
            "docs": s.docs,
            "chat_models": list(s.chat_models),
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
