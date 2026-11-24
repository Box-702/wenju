"""Per-generation model options from configured BYOK providers."""

from __future__ import annotations

from typing import Any


def options(store: Any, kind: str) -> list[dict[str, str]]:
    if kind not in {"image", "video"}:
        raise ValueError("只支持 image 或 video")
    out = []
    for row in store.list_api_providers(kind=kind):
        if not row.enabled or not row.api_key:
            continue
        config = row.config or {}
        models = [str(item.get("model")) for item in (config.get("models") or [])
                  if isinstance(item, dict) and item.get("model") and item.get("enabled") is not False]
        if not models and not config.get("models") and config.get("model"):
            models = [str(config["model"])]
        if not models and not config.get("models"):
            from ...llm.registry import _from_db_row
            default = _from_db_row(row).model
            models = [default] if default else []
        for model in dict.fromkeys(models):
            out.append({"provider_id": row.id, "provider": row.name,
                        "label": row.label or row.name, "model": model})
    return out


def resolve(store: Any, kind: str, provider_id: str, model_name: str) -> tuple[Any, str]:
    matches = [o for o in options(store, kind)
               if o["provider_id"] == provider_id and o["model"] == model_name]
    if not matches:
        raise ValueError("所选模型未配置或已停用，请重新查询可用模型")
    return store.get_api_provider(provider_id), model_name
