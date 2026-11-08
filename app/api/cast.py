# =====================================================================
# cast.py —— 资产库 API（角色形象 / 声音 / 图片 / 场景 的唯一入口）
#
# 三个端点：
#   GET   /projects/{id}/library          读：角色 + 场景 + 音色选项 + 告警
#   PATCH /projects/{id}/library          改：性别 / 音色 / 外貌 / 场景描述 / 图
#   POST  /projects/{id}/library/preview  试听：用某个音色读一句样音
#
# 为什么要有「试听」：音色是一串 voice_id（male-qn-badao），用户没法凭名字
# 判断「磁性男声」到底像不像这个角色。让人先听一句，再决定，比事后在成片里
# 发现「男主是御姐音」便宜得多。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from . import common, deps
from ..media.library import normalize_visual_style, apply_library_patch, build_library, voice_options
from ..media.casting import VOICE_LABELS

router = APIRouter()
log = logging.getLogger(__name__)

# 试听样音：拿角色自己的一句台词最好，没有台词时用这句占位。
SAMPLE_TEXT = "这句话，是用我的声音说的。"


class CharacterChange(BaseModel):
    gender: str | None = None
    voice_id: str | None = None
    appearance: str | None = None
    image: str | None = None


class LocationChange(BaseModel):
    description: str | None = None
    image: str | None = None


class LibraryPatch(BaseModel):
    characters: dict[str, CharacterChange] = Field(default_factory=dict)
    locations: dict[str, LocationChange] = Field(default_factory=dict)


class VoicePreview(BaseModel):
    voice_id: str = Field(min_length=1)
    text: str = ""
    gender: str | None = None


def _script_of(
    store: Any, project: Any, version_id: str | None = None, *, required: bool = True
) -> Any:
    """取项目的剧本版本。

    ``required=False`` 时，没有剧本版本返回 None 而不是抛错——**刚建的项目本来就没有
    剧本**，那是正常空态，不该让前端弹红色错误条（前端已有「还没有资产」的空态渲染）。
    需要真正报错的场景（改一个不存在的库）用 ``required=True``。
    """
    vid = version_id or project.current_version_id
    sv = store.get_version(vid) if vid else None
    script = getattr(sv, "script", None) if sv else None
    if script is None:
        if required:
            raise HTTPException(404, "该项目还没有可用的剧本版本，无法建立资产库")
        return None
    return script


@router.get("/projects/{project_id}/library")
def get_library(project_id: str, version_id: str | None = None) -> dict[str, Any]:
    """资产库总览：每个角色的形象锚、定妆图、性别与音色（含判定依据）。

    还没有剧本版本时返回**空资产库**而不是 404：新建的项目本来就还没剧本，
    那是正常空态，前端也有对应的空态渲染。
    """
    store = deps.store()
    project = common.get_project(project_id)
    style = store.get_video_style(project_id) or {}
    script = _script_of(store, project, version_id, required=False)
    if script is None:
        return {
            "characters": [],
            "locations": [],
            "warnings": [],
            "visual_style": normalize_visual_style(style.get("visual_style")),
        }
    return build_library(script, style, visual_style=style.get("visual_style"))


@router.get("/library/all")
def get_library_all() -> dict[str, Any]:
    """全部作品的资产（资产库「全部作品」范围）。

    为什么按作品分组返回：资产的身份边界就是作品——定妆照锚的是「这个剧本里的
    这个角色」，音色同理；混成一个平表就分不清哪个林然属于哪部戏。
    """
    store = deps.store()
    projects: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    for p in store.list_projects():
        version = store.latest_version(p)
        if version is not None:
            style = store.get_video_style(p.id) or {}
            lib = build_library(version.script, style, visual_style=style.get("visual_style"))
            projects.append({
                "project_id": p.id,
                "title": lib.get("project_title") or p.title,
                "visual_style": lib.get("visual_style", ""),
                "characters": lib.get("characters", []),
                "locations": lib.get("locations", []),
                "warnings": lib.get("warnings", []),
            })
        for a in store.list_assets(p.id):
            assets.append({
                "id": a.id,
                "project_id": p.id,
                "project_title": p.title,
                "kind": a.kind,
                "name": a.name,
                "source": a.source,
                "url": a.url,
                "created_at": a.created_at.isoformat() if a.created_at else "",
            })
    return {"projects": projects, "assets": assets}


@router.patch("/projects/{project_id}/library")
def patch_library(project_id: str, payload: LibraryPatch) -> dict[str, Any]:
    """人工修正资产：指定性别/音色/外貌/图。返回修正后的整张资产库。"""
    store = deps.store()
    project = common.get_project(project_id)
    script = _script_of(store, project, None)

    patch = {
        "characters": {
            name: ch.model_dump(exclude_none=True)
            for name, ch in payload.characters.items()
        },
        "locations": {
            name: loc.model_dump(exclude_none=True)
            for name, loc in payload.locations.items()
        },
    }
    style = store.get_video_style(project_id) or {}
    # 传 script：patch 的键是人名，落库前要换成角色 id（名字会被改，id 不会）。
    merged = apply_library_patch(style, patch, script)
    store.set_video_style(project_id, merged)

    # 记一条给人看的日志：改了什么、改到哪个角色的声音上。
    for name, ch in payload.characters.items():
        if ch.voice_id:
            log.info("资产库：%s 的音色指定为 %s（%s）", name,
                     ch.voice_id, VOICE_LABELS.get(ch.voice_id, ch.voice_id))
    return build_library(script, merged, visual_style=merged.get("visual_style"))


@router.get("/projects/{project_id}/library/voices")
def list_voices(project_id: str) -> list[dict[str, str]]:
    """可选音色清单（按项目类型排序：漫剧把年轻音色排前面）。"""
    project = common.get_project(project_id)
    style = deps.store().get_video_style(project.id) or {}
    return voice_options(style.get("visual_style"))


@router.post("/projects/{project_id}/library/preview")
def preview_voice(project_id: str, payload: VoicePreview) -> Response:
    """试听：用指定音色读一句样音，返回 mp3 字节流。"""
    common.get_project(project_id)
    from ..llm.registry import resolve_tts
    from ..llm.tts import build_tts_client
    from ..config import get_settings

    client = build_tts_client(resolve_tts(get_settings()))
    if client is None or not client.available:
        raise HTTPException(503, "未配置 TTS（需要 MINIMAX_API_KEY），无法试听")

    text = (payload.text or "").strip()[:80] or SAMPLE_TEXT
    try:
        audio = client.synthesize(text, voice_id=payload.voice_id, speed=1.0)
    except Exception as e:  # noqa: BLE001
        log.warning("试听失败：%s", e)
        raise HTTPException(502, f"试听失败：{e}") from e
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Cache-Control": "no-store"})
