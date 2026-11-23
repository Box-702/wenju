"""Character identity and per-shot look selection (D-80)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .assets import build_index, rekey


def base_id(character_id: str) -> str:
    return f"look_{character_id}_base"


def all_looks(style: dict[str, Any], script: Any) -> dict[str, dict[str, Any]]:
    """Expose legacy character images as explicit base looks without rewriting them."""
    idx = build_index(script)
    images = rekey(style.get("reference_images") or {}, idx)
    descriptions = rekey(style.get("character_appearances") or {}, idx)
    result: dict[str, dict[str, Any]] = {}
    for char in script.characters:
        cid = char.id
        if images.get(cid) or descriptions.get(cid):
            lid = base_id(cid)
            result[lid] = {"id": lid, "character_id": cid, "name": "基础造型",
                           "description": descriptions.get(cid, ""), "image": images.get(cid, ""),
                           "available_from_shot_id": "", "status": "ready" if images.get(cid) else "draft"}
    for lid, raw in (style.get("character_looks") or {}).items():
        if isinstance(raw, dict):
            result[str(lid)] = {**raw, "id": str(lid)}
    return result


def register_generated_images(style: dict[str, Any], script: Any,
                              refs: dict[str, str]) -> dict[str, Any]:
    """Register new character generations without replacing an existing look."""
    idx = build_index(script)
    current = all_looks(style, script)
    saved = dict(style.get("character_looks") or {})
    legacy = rekey(style.get("reference_images") or {}, idx)
    for raw_id, url in refs.items():
        if raw_id in saved:
            saved[raw_id] = {**saved[raw_id], "image": url, "status": "ready"}
            current[raw_id] = saved[raw_id]
            continue
        cid = idx.resolve(raw_id) or raw_id
        if cid not in {c.id for c in script.characters}:
            legacy[cid] = url
            continue
        existing = [v for v in current.values() if v.get("character_id") == cid and v.get("image")]
        lid = f"look_{uuid4().hex[:12]}" if existing else base_id(cid)
        saved[lid] = {"id": lid, "character_id": cid,
                      "name": "新生成造型" if existing else "基础造型",
                      "description": (style.get("character_appearances") or {}).get(cid, ""),
                      "image": url, "available_from_shot_id": "", "status": "ready"}
        current[lid] = saved[lid]
    return {"reference_images": legacy, "character_looks": saved}


def selected_look(*, character_id: str, scene: Any, shot: Any,
                  looks: dict[str, dict[str, Any]], shot_plan: list[Any],
                  style: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fail closed on ambiguous or premature selections; never pick the newest image."""
    style = style or {}
    explicit = (style.get("shot_look_ids") or {}).get(shot.id, {}).get(character_id) or (getattr(shot, "character_look_ids", None) or {}).get(character_id)
    scene_choice = (style.get("scene_look_ids") or {}).get(scene.id, {}).get(character_id) or (getattr(scene, "character_look_ids", None) or {}).get(character_id)
    lid = explicit or scene_choice or base_id(character_id)
    look = looks.get(lid)
    if not look and not explicit and not scene_choice and not any(
        item.get("character_id") == character_id for item in looks.values()
    ):
        return {}  # 旧项目尚未给该角色生成定妆图
    if not look or look.get("character_id") != character_id:
        raise ValueError(f"角色 {character_id} 的造型不明确，请为镜头 {shot.id} 选择造型")
    start = str(look.get("available_from_shot_id") or "")
    if start:
        order = {s.id: i for i, s in enumerate(shot_plan)}
        if start not in order or shot.id not in order or order[shot.id] < order[start]:
            raise ValueError(f"造型「{look.get('name') or lid}」只能从镜头 {start} 开始使用")
    return look
