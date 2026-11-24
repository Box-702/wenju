"""Location image variants and deterministic scene/shot selection."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .assets import build_index, rekey


def base_id(location_id: str) -> str:
    return f"location_state_{location_id}_base"


def all_states(style: dict[str, Any], script: Any) -> dict[str, dict[str, Any]]:
    idx = build_index(script)
    images = rekey(style.get("reference_images") or {}, idx)
    descriptions = rekey(style.get("environment_descriptions") or {}, idx)
    result = {}
    for loc in script.locations:
        if images.get(loc.id) or descriptions.get(loc.id):
            sid = base_id(loc.id)
            result[sid] = {"id": sid, "location_id": loc.id, "name": "基础状态",
                           "description": descriptions.get(loc.id, ""),
                           "image": images.get(loc.id, ""), "status": "ready" if images.get(loc.id) else "draft"}
    for sid, raw in (style.get("location_states") or {}).items():
        if isinstance(raw, dict):
            result[str(sid)] = {**raw, "id": str(sid)}
    return result


def register_generated_images(style: dict[str, Any], script: Any,
                              refs: dict[str, str]) -> dict[str, Any]:
    idx = build_index(script)
    saved = dict(style.get("location_states") or {})
    legacy = rekey(style.get("reference_images") or {}, idx)
    current = all_states(style, script)
    locations = {loc.id for loc in script.locations}
    for raw_id, url in refs.items():
        if raw_id in saved:
            saved[raw_id] = {**saved[raw_id], "image": url, "status": "ready"}
            continue
        lid = idx.resolve(raw_id) or raw_id
        if lid not in locations:
            continue
        existing = any(s.get("location_id") == lid and s.get("image") for s in current.values())
        sid = f"location_state_{uuid4().hex[:12]}" if existing else base_id(lid)
        saved[sid] = {"id": sid, "location_id": lid,
                      "name": "新生成状态" if existing else "基础状态",
                      "description": (style.get("environment_descriptions") or {}).get(lid, ""),
                      "image": url, "status": "ready"}
        if not existing:
            legacy[lid] = url
    return {"reference_images": legacy, "location_states": saved}


def selected_state(*, location_id: str, scene_id: str, shot_id: str,
                   states: dict[str, dict[str, Any]], style: dict[str, Any]) -> dict[str, Any]:
    explicit = (style.get("shot_location_state_ids") or {}).get(shot_id)
    scene_choice = (style.get("scene_location_state_ids") or {}).get(scene_id)
    sid = explicit or scene_choice or base_id(location_id)
    state = states.get(sid)
    if not state and not explicit and not scene_choice and not any(
        s.get("location_id") == location_id for s in states.values()
    ):
        return {}
    if not state or state.get("location_id") != location_id:
        raise ValueError(f"地点 {location_id} 的状态不明确，请为镜头 {shot_id} 选择场景状态")
    return state
