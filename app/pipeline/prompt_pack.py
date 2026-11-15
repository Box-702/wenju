# =====================================================================
# prompt_pack.py —— 提示词包导出（POSITIONING P0）
#
# 提示词包 = 「把这套片子怎么拍说清楚」的一份可交付文档：每镜的结构化
# 字段（景别 / 运镜 / 动作 / 空间关系 / 终帧）+ 逐字提示词 + 一致性约束
# （人物锚 / 环境锚 / 参考素材）+ 首帧图提示词。
#
# 给谁用：交给别的视频模型 / 别的团队 / 自己的剪辑台。剪映草稿那一份
# 是「按秒排好的素材清单」，可以直接铺进剪映的时间线（对齐 mediago 的
# draft_content.json 双文件做法，见 docs/PLAN-mediago-reference.md）。
#
# 与 app/pipeline/export.py 的区别：export.py 导的是**剧本**（台本），
# 这里导的是**镜头怎么拍**（分镜 + 提示词 + 锚）。
# =====================================================================

from __future__ import annotations

import json
from typing import Any

from ..domain import StyleGuide

# 景别 / 运镜 的中文名（导出给外部用，别把内部枚举直接抛出去）
SHOT_TYPE_ZH = {
    "extreme_wide": "极远景", "wide": "远景", "medium": "中景",
    "close_up": "近景", "extreme_close_up": "特写",
    "over_shoulder": "过肩", "pov": "主观",
}
CAMERA_TYPE_ZH = {
    "static": "固定", "pan": "摇", "tilt": "俯仰", "dolly": "推拉",
    "tracking": "跟", "crane": "升降", "handheld": "手持", "zoom": "变焦",
    "orbit": "环绕",
}
SPEED_ZH = {"slow": "慢", "medium": "中", "fast": "快"}
HEIGHT_ZH = {"low": "低机位", "eye": "平视", "high": "高机位", "top": "俯视"}


def _camera_text(cam: dict[str, Any]) -> str:
    if not isinstance(cam, dict):
        return ""
    bits = [CAMERA_TYPE_ZH.get(str(cam.get("type") or ""), str(cam.get("type") or ""))]
    if cam.get("direction"):
        bits.append(str(cam["direction"]))
    if cam.get("speed"):
        bits.append(SPEED_ZH.get(str(cam["speed"]), str(cam["speed"])))
    if cam.get("height"):
        bits.append(HEIGHT_ZH.get(str(cam["height"]), str(cam["height"])))
    if cam.get("path"):
        bits.append(f"轨迹 {cam['path']}")
    return " · ".join(b for b in bits if b)


def _shot_entry(shot: dict[str, Any]) -> dict[str, Any]:
    """一个机位在提示词包里长什么样。"""
    prompt = str(shot.get("group_prompt") or shot.get("video_prompt") or "")
    cam = shot.get("camera") if isinstance(shot.get("camera"), dict) else {}
    return {
        "shot_id": shot.get("id"),
        "episode_id": shot.get("episode_id") or "",
        "episode_index": shot.get("episode_index") or 0,
        "scene_id": shot.get("scene_id") or "",
        "group_id": shot.get("group_id") or "",
        "group_index": shot.get("group_index") or 0,
        "order": shot.get("order"),
        "shot_type": shot.get("shot_type") or "",
        "shot_type_zh": SHOT_TYPE_ZH.get(str(shot.get("shot_type") or ""), ""),
        "subject": shot.get("subject") or "",
        "action": shot.get("action") or "",
        "camera": _camera_text(cam),
        "duration_sec": shot.get("duration_sec"),
        "dialogue_at": shot.get("dialogue_at"),
        "dialogue_ref": shot.get("dialogue_ref") or "",
        "lighting": shot.get("lighting") or "",
        "mood": shot.get("mood") or "",
        "spatial": shot.get("spatial") or "",
        "background_action": shot.get("background_action") or "",
        "final_frame": shot.get("final_frame") or "",
        "cut_reason": shot.get("cut_reason") or "",
        "function": shot.get("function") or "",
        "prompt": prompt,
        "reference_asset_ids": list(shot.get("reference_asset_ids") or []),
        "reference_images": list(shot.get("reference_images") or []),
    }


def build_prompt_pack(
    shots: list[dict[str, Any]],
    style_guide: StyleGuide | None,
    *,
    project_title: str = "",
    visual_style: str = "",
) -> dict[str, Any]:
    """组装提示词包（结构化，不渲染）。

    集-镜两级（D-63）：按集分组，集内按 order 排。
    """
    from ..media.refs import character_prompt, environment_prompt

    entries = [_shot_entry(s) for s in shots
               if isinstance(s, dict) and s.get("id")]
    entries.sort(key=lambda e: (e.get("episode_index") or 0, e.get("order") or 0))

    episodes: list[dict[str, Any]] = []
    for e in entries:
        key = e.get("episode_id") or ""
        if not episodes or episodes[-1]["episode_id"] != key:
            episodes.append({
                "episode_id": key,
                "episode_index": e.get("episode_index") or 0,
                "shots": [],
            })
        episodes[-1]["shots"].append(e)

    anchors: dict[str, Any] = {"characters": {}, "locations": {}}
    if style_guide is not None:
        for cid, text in (style_guide.character_appearances or {}).items():
            anchors["characters"][cid] = {
                "appearance": text,
                "first_frame_prompt": character_prompt(
                    cid, text, style_guide.visual_style,
                ),
            }
        for lid, text in (style_guide.environment_descriptions or {}).items():
            anchors["locations"][lid] = {
                "description": text,
                "first_frame_prompt": environment_prompt(
                    lid, text, style_guide.visual_style,
                ),
            }

    return {
        "title": project_title,
        "visual_style": visual_style or (style_guide.visual_style if style_guide else ""),
        "episodes": episodes,
        "anchors": anchors,
        "shot_count": len(entries),
        "total_seconds": round(
            sum(float(e.get("duration_sec") or 0) for e in entries), 1,
        ),
    }


# ---------- 渲染 ----------


def render_markdown(pack: dict[str, Any]) -> str:
    """提示词包（Markdown）——人读的那一份。"""
    lines: list[str] = [f"# 提示词包 · {pack.get('title') or '未命名'}"]
    meta = [f"画风：{pack.get('visual_style') or '（未设）'}",
            f"镜头：{pack.get('shot_count') or 0} 个",
            f"合计：{pack.get('total_seconds') or 0} 秒"]
    lines.append("　·　".join(meta))
    lines.append("")

    anchors = pack.get("anchors") or {}
    chars = anchors.get("characters") or {}
    locs = anchors.get("locations") or {}
    if chars or locs:
        lines.append("## 一致性约束（下游逐字复用，不许改写）")
        lines.append("")
        if chars:
            lines.append("### 人物锚")
            for cid, a in chars.items():
                lines.append(f"- **{cid}**：{a.get('appearance') or ''}")
                if a.get("first_frame_prompt"):
                    lines.append(f"  - 首帧图提示词：{a['first_frame_prompt']}")
            lines.append("")
        if locs:
            lines.append("### 环境锚")
            for lid, a in locs.items():
                lines.append(f"- **{lid}**：{a.get('description') or ''}")
                if a.get("first_frame_prompt"):
                    lines.append(f"  - 首帧图提示词：{a['first_frame_prompt']}")
            lines.append("")

    lines.append("## 分镜")
    lines.append("")
    for ep in pack.get("episodes") or []:
        title = ep.get("episode_id") or "（未分集）"
        lines.append(f"### 第 {int(ep.get('episode_index') or 0) + 1} 集 · {title}")
        lines.append("")
        for e in ep.get("shots") or []:
            head = " / ".join(
                x for x in [
                    e.get("shot_type_zh") or e.get("shot_type"),
                    e.get("camera"),
                    f"{e.get('duration_sec')}s",
                ] if x
            )
            lines.append(f"#### {e.get('shot_id')}　{head}")
            if e.get("group_id"):
                lines.append(
                    f"- 镜头组：{e['group_id']}（机位 {int(e.get('group_index') or 0) + 1}）"
                )
            for label, key in (
                ("主体", "subject"), ("动作", "action"), ("空间", "spatial"),
                ("背景动作", "background_action"), ("光线", "lighting"),
                ("情绪", "mood"), ("终帧", "final_frame"),
                ("切镜理由", "cut_reason"), ("功能", "function"),
            ):
                if e.get(key):
                    lines.append(f"- {label}：{e[key]}")
            if e.get("dialogue_at") is not None:
                lines.append(f"- 台词开口：第 {e['dialogue_at']} 秒")
            if e.get("reference_asset_ids"):
                lines.append(f"- 参考素材：{'、'.join(e['reference_asset_ids'])}")
            lines.append("")
            lines.append("```")
            lines.append(e.get("prompt") or "（无提示词）")
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def render_json(pack: dict[str, Any]) -> str:
    """提示词包（JSON）——给别的工具读的那一份。"""
    return json.dumps(pack, ensure_ascii=False, indent=2)


def render_jianying(pack: dict[str, Any]) -> dict[str, Any]:
    """剪映草稿（draft_content.json 的内容 + 素材清单）。

    对齐 mediago 的做法（docs/PLAN-mediago-reference.md §五）：素材清单 +
    时间线草稿。这里合成一个 JSON 返回，由调用方落盘。
    """
    materials: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    for ep in pack.get("episodes") or []:
        for e in ep.get("shots") or []:
            dur = float(e.get("duration_sec") or 0)
            mid = f"mat_{e.get('shot_id')}"
            materials.append({
                "material_id": mid,
                "shot_id": e.get("shot_id"),
                "episode_id": e.get("episode_id") or "",
                "shot_type": e.get("shot_type") or "",
                "camera": e.get("camera") or "",
                "duration_sec": dur,
                "reference_asset_ids": e.get("reference_asset_ids") or [],
            })
            timeline.append({
                "material_id": mid,
                "target_timerange": {
                    "start": round(cursor, 3),
                    "duration": round(dur, 3),
                },
            })
            cursor += dur
    return {
        "version": "1.0",
        "duration": round(cursor, 3),
        "fps": 24,
        "materials": materials,
        "tracks": [{"type": "video", "segments": timeline}],
    }
