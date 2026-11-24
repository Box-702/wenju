"""Single-asset image canvas options and prompt assembly."""

from __future__ import annotations

from typing import Any

from .refs import character_prompt, environment_prompt


_SIZES = {
    "1K": {"1:1": "1024x1024", "16:9": "1360x768", "9:16": "768x1360",
           "4:3": "1184x888", "3:4": "888x1184"},
    "2K": {"1:1": "2048x2048", "16:9": "2720x1536", "9:16": "1536x2720",
           "4:3": "2368x1776", "3:4": "1776x2368"},
}


def image_model_options(store: Any) -> list[dict[str, Any]]:
    """Only configured models; expose settings supported by our actual adapter."""
    result = []
    for row in store.list_api_providers(kind="image"):
        if not row.enabled or not row.api_key:
            continue
        config = row.config or {}
        models = [str(item["model"]) for item in config.get("models") or []
                  if isinstance(item, dict) and item.get("model") and item.get("enabled") is not False]
        if not models and not config.get("models"):
            models = [str(config.get("model") or "")]
        for model in dict.fromkeys(m for m in models if m):
            capable = row.name in {"minimax", "seedance"}
            result.append({
                "provider_id": row.id, "provider": row.name,
                "label": row.label or row.name, "model_name": model,
                "ratios": list(_SIZES["1K"]) if capable else ["1:1"],
                "qualities": ["1K", "2K"] if capable else ["1K"],
            })
    return result


def output_size(provider: str, ratio: str, quality: str) -> str:
    if provider not in {"minimax", "seedance"} and (ratio, quality) != ("1:1", "1K"):
        raise ValueError("这个模型目前只支持 1:1 · 1K")
    try:
        return _SIZES[quality][ratio]
    except KeyError as exc:
        raise ValueError("不支持所选比例或清晰度") from exc


def canvas_prompt(*, kind: str, name: str, description: str, request: str,
                  visual_style: str | None, identity_anchor: str = "") -> str:
    detail = "；".join(part for part in (description.strip(), request.strip()) if part)
    if kind == "character":
        return character_prompt(name, detail, visual_style, identity_anchor=identity_anchor)
    if kind == "location":
        return environment_prompt(name, detail, visual_style)
    raise ValueError("只支持角色或场景")
