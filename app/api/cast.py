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
from uuid import uuid4
from typing import Any

from fastapi import APIRouter, HTTPException, Response, UploadFile, File, Form
from pydantic import BaseModel, Field

from . import common, deps
from ..config import get_settings
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
    note: str | None = None        # 画面要求（拼进定妆照 prompt）
    voice_note: str | None = None   # 声音描述（克隆音色时的参考文本）


class LocationChange(BaseModel):
    description: str | None = None
    image: str | None = None
    note: str | None = None         # 画面要求（拼进场景图 prompt）


class LibraryPatch(BaseModel):
    characters: dict[str, CharacterChange] = Field(default_factory=dict)
    locations: dict[str, LocationChange] = Field(default_factory=dict)


class LookCreate(BaseModel):
    character_id: str
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    image: str = ""
    available_from_shot_id: str = ""
    identity_anchor: str = ""  # 同角色所有造型共用的面部特征


class LookSelection(BaseModel):
    character_id: str
    look_id: str


class IdentityAnchor(BaseModel):
    description: str = Field(min_length=1)


@router.put("/projects/{project_id}/characters/{character_id}/identity-anchor")
def set_identity_anchor(project_id: str, character_id: str, payload: IdentityAnchor) -> dict[str, Any]:
    store = deps.store()
    project = common.get_project(project_id)
    script = _script_of(store, project)
    if character_id not in {c.id for c in script.characters}:
        raise HTTPException(404, "角色不存在")
    style = store.get_video_style(project_id) or {}
    anchors = dict(style.get("character_identity_anchors") or {})
    anchors[character_id] = payload.description.strip()
    store.merge_video_style(project_id, {"character_identity_anchors": anchors})
    return {"character_id": character_id, "description": anchors[character_id]}


@router.post("/projects/{project_id}/looks")
def create_look(project_id: str, payload: LookCreate) -> dict[str, Any]:
    from ..media.looks import all_looks

    store = deps.store()
    project = common.get_project(project_id)
    script = _script_of(store, project)
    if payload.character_id not in {c.id for c in script.characters}:
        raise HTTPException(404, "角色不存在")
    versions = store.list_video_versions(project_id)
    shot_ids = {s.get("id") for v in versions for s in v.shots if isinstance(s, dict)}
    if not payload.available_from_shot_id or payload.available_from_shot_id not in shot_ids:
        raise HTTPException(400, "新造型需指定首次可用镜头，避免剧情变化应用到之前的镜头")
    style = store.get_video_style(project_id) or {}
    anchors = dict(style.get("character_identity_anchors") or {})
    anchor = payload.identity_anchor.strip()
    if anchor and anchors.get(payload.character_id) and anchors[payload.character_id] != anchor:
        raise HTTPException(409, "该角色已有面部身份锚，请沿用同一描述")
    if anchor:
        anchors[payload.character_id] = anchor
    lid = f"look_{uuid4().hex[:12]}"
    look = {"id": lid, **payload.model_dump(exclude={"identity_anchor"}),
            "status": "ready" if payload.image else "draft"}
    looks = dict(style.get("character_looks") or {})
    looks[lid] = look
    store.merge_video_style(project_id, {"character_looks": looks,
                                         "character_identity_anchors": anchors})
    return look


@router.put("/projects/{project_id}/looks/{look_id}/image")
def set_look_image(project_id: str, look_id: str, payload: dict[str, str]) -> dict[str, Any]:
    store = deps.store()
    common.get_project(project_id)
    style = store.get_video_style(project_id) or {}
    looks = dict(style.get("character_looks") or {})
    if look_id not in looks:
        raise HTTPException(404, "造型不存在")
    image = str(payload.get("image") or "").strip()
    if not image:
        raise HTTPException(400, "图片地址不能为空")
    looks[look_id] = {**looks[look_id], "image": image, "status": "ready"}
    store.merge_video_style(project_id, {"character_looks": looks})
    return looks[look_id]


@router.put("/projects/{project_id}/{scope}/{target_id}/look")
def select_look(project_id: str, scope: str, target_id: str, payload: LookSelection) -> dict[str, Any]:
    from ..media.looks import all_looks, selected_look
    from ..domain import Shot

    store = deps.store()
    project = common.get_project(project_id)
    script = _script_of(store, project)
    style = store.get_video_style(project_id) or {}
    if scope not in {"scene", "shot"}:
        raise HTTPException(404, "选型范围不存在")
    looks = all_looks(style, script)
    if payload.look_id not in looks or looks[payload.look_id].get("character_id") != payload.character_id:
        raise HTTPException(400, "造型不属于该角色")
    if scope == "scene":
        scene = next((s for s in script.scenes if s.id == target_id), None)
        if scene is None or payload.character_id not in scene.characters:
            raise HTTPException(404, "场景中没有该角色")
        version = store.get_video_version(project.current_video_version_id) if project.current_video_version_id else None
        if version:
            plan = [Shot.model_validate(s) for s in version.shots]
            try:
                for candidate in plan:
                    if candidate.scene_id == target_id:
                        selected_look(character_id=payload.character_id, scene=scene, shot=candidate,
                                      looks=looks, shot_plan=plan,
                                      style={"scene_look_ids": {target_id: {payload.character_id: payload.look_id}}})
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        key = "scene_look_ids"
    else:
        version = store.get_video_version(project.current_video_version_id) if project.current_video_version_id else None
        shot = next((s for s in (version.shots if version else []) if s.get("id") == target_id), None)
        if shot is None:
            raise HTTPException(404, "镜头不存在")
        scene = next((s for s in script.scenes if s.id == shot.get("scene_id")), None)
        if scene is None or payload.character_id not in scene.characters:
            raise HTTPException(400, "镜头所在场景中没有该角色")
        try:
            selected_look(character_id=payload.character_id, scene=scene, shot=Shot.model_validate(shot),
                          looks=looks, shot_plan=[Shot.model_validate(s) for s in version.shots],
                          style={"shot_look_ids": {target_id: {payload.character_id: payload.look_id}}})
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        key = "shot_look_ids"
    mapping = {k: dict(v) for k, v in (style.get(key) or {}).items()}
    mapping.setdefault(target_id, {})[payload.character_id] = payload.look_id
    store.merge_video_style(project_id, {key: mapping})
    return {"ok": True, "look_id": payload.look_id}


@router.post("/projects/{project_id}/looks/{look_id}/generate")
def generate_look_image(project_id: str, look_id: str) -> dict[str, Any]:
    """Generate this look only. Text-to-image cannot guarantee the same face as another look."""
    from ..domain import StyleGuide
    from ..media.refs import build_reference_assets

    store = deps.store()
    project = common.get_project(project_id)
    script = _script_of(store, project)
    style = store.get_video_style(project_id) or {}
    look = (style.get("character_looks") or {}).get(look_id)
    if not look:
        raise HTTPException(404, "造型不存在")
    cid = look["character_id"]
    runner = deps.subagent_runner()

    def _run(task: object) -> str:
        current = store.get_video_style(project_id) or {}
        guide_data = dict(current)
        guide_data["character_appearances"] = {cid: look["description"]}
        refs = build_reference_assets(script, StyleGuide.model_validate(guide_data),
                                      entities=[cid], regenerate=True, look_ids=[look_id],
                                      on_progress=lambda m: runner.update_step(task.id, m, "running"))
        url = refs.get(look_id)
        if not url:
            raise ValueError("没有生成造型图片")
        fresh = dict(store.get_video_style(project_id) or {})
        looks = dict(fresh.get("character_looks") or {})
        looks[look_id] = {**looks[look_id], "image": url, "status": "ready"}
        store.merge_video_style(project_id, {"character_looks": looks})
        return f"造型「{look['name']}」已生成；请检查与其他造型的脸部一致性"

    task_id = runner.start("生成角色造型", _run, project_id=project_id)
    return {"task_id": task_id, "started": True}


class CastProfileIn(BaseModel):
    """演员库条目。"""

    name: str = Field(min_length=1)
    gender: str = "unknown"
    appearance: str = ""
    voice_id: str = ""
    image_asset_id: str = ""
    notes: str = ""


class ApplyCastProfile(BaseModel):
    """把演员库的一条应用到本作的某个角色。"""

    profile_id: str = Field(min_length=1)
    entity: str = Field(min_length=1)   # 角色名或 id


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


def _shot_clips(store: Any, project_id: str) -> list[dict[str, Any]]:
    """已成功的镜头视频，作为资产列出（source='shot'）。

    单镜视频不在 media_assets 里（在 video_jobs），但它同样是这部作品的产出，
    资产库要能一眼看到、能下载。
    """
    out: list[dict[str, Any]] = []
    for j in store.list_video_jobs(project_id):
        if j.status != "succeeded" or not j.video_url:
            continue
        out.append({
            "id": j.id,
            "project_id": project_id,
            "kind": "video",
            "name": f"镜头 {j.shot_id}",
            "source": "shot",
            "url": j.video_url,
            "shot_id": j.shot_id,
            "created_at": j.created_at.isoformat() if j.created_at else "",
        })
    return out


def _voice_pool(store: Any, visual_style: str | None = None) -> list[dict[str, Any]]:
    """下游用的完整音色池（勾选优先 + 内置兜底 + 克隆）。见 casting.build_voice_pool。"""
    from ..media.casting import build_voice_pool

    return build_voice_pool(store, visual_style)


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
    # 风格指南正文此前只在库里，界面上只露出一个 visual_style 标签——
    # 色板、灯光、运镜风格、每个角色的外貌锚、每个场景的环境锚全都看不见，
    # 而这些正是下游提示词逐字复用的锚。
    style_guide = {
        "visual_style": normalize_visual_style(style.get("visual_style")),
        "color_palette": style.get("color_palette") or [],
        "lighting_style": style.get("lighting_style") or "",
        "camera_style": style.get("camera_style") or "",
        "character_appearances": style.get("character_appearances") or {},
        "environment_descriptions": style.get("environment_descriptions") or {},
    }
    if script is None:
        return {
            "characters": [],
            "locations": [],
            "warnings": [],
            "visual_style": style_guide["visual_style"],
            "style_guide": style_guide,
            "clips": _shot_clips(store, project_id),
        }
    lib = build_library(script, style, visual_style=style.get("visual_style"),
                        voice_pool=_voice_pool(store, style.get("visual_style")))
    lib["clips"] = _shot_clips(store, project_id)
    lib["style_guide"] = style_guide
    lib["look_selections"] = {"scenes": style.get("scene_look_ids") or {},
                              "shots": style.get("shot_look_ids") or {}}
    lib["scene_characters"] = {sc.id: sc.characters for sc in script.scenes}
    for char in lib["characters"]:
        char["identity_anchor"] = (style.get("character_identity_anchors") or {}).get(char["char_id"], "")
    return lib


def _profile_dict(p: Any) -> dict[str, Any]:
    return {
        "id": p.id, "name": p.name, "gender": p.gender,
        "appearance": p.appearance, "voice_id": p.voice_id,
        "image_asset_id": p.image_asset_id, "notes": p.notes,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/cast-profiles")
def list_cast_profiles() -> list[dict[str, Any]]:
    """演员库（跨项目复用的角色身份）。"""
    return [_profile_dict(p) for p in deps.store().list_cast_profiles()]


@router.post("/cast-profiles")
def create_cast_profile(payload: CastProfileIn) -> dict[str, Any]:
    """新建 / 更新演员库条目（带 id 则更新那条）。"""
    store = deps.store()
    p = store.save_cast_profile(
        name=payload.name.strip(), gender=payload.gender,
        appearance=payload.appearance, voice_id=payload.voice_id,
        image_asset_id=payload.image_asset_id, notes=payload.notes,
    )
    return _profile_dict(p)


@router.delete("/cast-profiles/{profile_id}")
def delete_cast_profile(profile_id: str) -> dict[str, Any]:
    """删除演员库条目。"""
    if not deps.store().delete_cast_profile(profile_id):
        raise HTTPException(404, "演员库条目不存在")
    return {"id": profile_id, "deleted": True}


@router.post("/projects/{project_id}/library/apply-profile")
def apply_cast_profile(project_id: str, payload: ApplyCastProfile) -> dict[str, Any]:
    """把演员库里的一条应用到本作的某个角色（外貌锚 / 音色 / 定妆照）。

    角色 id 仍是作品内序号（char_001）——跨作品身份由演员库承担，
    应用只是把锚与图按引用落进这个作品（D-64）。
    """
    from ..media.assets import build_index
    from ..media.library import apply_library_patch

    store = deps.store()
    project = common.get_project(project_id)
    profile = store.get_cast_profile(payload.profile_id)
    if profile is None:
        raise HTTPException(404, "演员库条目不存在")
    version = store.latest_version(project)
    if version is None:
        raise HTTPException(400, "还没有剧本版本，先在对话里生成初稿")
    script = version.script
    idx = build_index(script)
    entity_id = idx.resolve(payload.entity)
    if not entity_id:
        raise HTTPException(404, f"剧本里没有「{payload.entity}」这个角色")
    char_ids = {c.id for c in script.characters}
    if entity_id not in char_ids:
        raise HTTPException(400, f"「{payload.entity}」不是角色（不能套演员库）")

    change: dict[str, Any] = {}
    if profile.appearance:
        change["appearance"] = profile.appearance
    if profile.voice_id:
        change["voice_id"] = profile.voice_id
    if profile.gender and profile.gender != "unknown":
        change["gender"] = profile.gender
    if profile.image_asset_id:
        asset = store.get_asset(profile.image_asset_id)
        if asset is None:
            raise HTTPException(404, "演员库引用的定妆图素材不存在")
        change["image"] = asset.url
    if not change:
        raise HTTPException(400, "这条演员库条目是空的，没什么可应用")

    style = store.get_video_style(project_id) or {}
    merged = apply_library_patch(style, {"characters": {payload.entity: change}}, script)
    store.set_video_style(project_id, merged)
    return {"ok": True, "entity_id": entity_id, "applied": sorted(change)}


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
            # 池按本项目画风排（内置 8 个的顺序=自动选角优先级，漫剧年轻在前）
            lib = build_library(version.script, style, visual_style=style.get("visual_style"),
                                voice_pool=_voice_pool(store, style.get("visual_style")))
            projects.append({
                "project_id": p.id,
                "title": lib.get("project_title") or p.title,
                "visual_style": lib.get("visual_style", ""),
                "characters": lib.get("characters", []),
                "locations": lib.get("locations", []),
                "warnings": lib.get("warnings", []),
                "clips": _shot_clips(store, p.id),
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
    return build_library(script, merged, visual_style=merged.get("visual_style"),
                         voice_pool=_voice_pool(store, merged.get("visual_style")))


@router.get("/projects/{project_id}/library/voices")
def list_voices(project_id: str) -> list[dict[str, Any]]:
    """可选音色清单（勾选过用勾选池；否则按项目画风排内置池）。"""
    project = common.get_project(project_id)
    style = deps.store().get_video_style(project.id) or {}
    return voice_options(style.get("visual_style"),
                         pool=_voice_pool(deps.store(), style.get("visual_style")))


@router.post("/voices/discover")
def discover_voices() -> dict[str, Any]:
    """拿当前配置的 key 去厂商问一遍有什么系统音色，落库供勾选（BYOK）。

    不猜、不硬编码：MiniMax 的 /v1/get_voice 返回这把 key 账号可用的全部音色。
    发现只写入池、不改勾选状态（重新发现不该把用户已勾的关掉或擅自启用新的）。
    性别从 voice_id 前缀判（厂商不给），推不出的归 other。
    """
    from ..config import get_settings
    from ..llm.registry import resolve_tts
    from ..llm.tts import build_tts_client

    store = deps.store()
    client = build_tts_client(resolve_tts(get_settings(), store))
    if client is None:
        raise HTTPException(503, "没有配置可用的 MiniMax API Key，无法查询音色列表")
    try:
        voices = client.list_system_voices()
    except NotImplementedError:
        # 厂商压根没实现这个查询——原始异常对用户无意义，501 的语义已完整
        raise HTTPException(501, "当前语音厂商不支持音色列表查询") from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"查询音色列表失败：{e}") from e
    if not voices:
        raise HTTPException(502, "厂商返回空的音色列表")
    store.upsert_voice_pool(voices)
    return {"count": len(voices), "voices": voices}


@router.get("/voices/pool")
def get_voice_pool() -> dict[str, Any]:
    """音色池：全部发现的（含 enabled 标记，供管理界面）+ 下游真正用的启用池。"""
    store = deps.store()
    all_rows = [
        {"voice_id": v.voice_id, "name": v.name, "gender": v.gender,
         "source": v.source, "enabled": v.enabled}
        for v in store.list_voice_pool()
    ]
    return {"all": all_rows, "enabled": store.enabled_voice_pool()}


class VoicePoolUpdate(BaseModel):
    voice_ids: list[str] = Field(default_factory=list)


@router.put("/voices/pool")
def put_voice_pool(payload: VoicePoolUpdate) -> dict[str, Any]:
    """保存勾选：把 voice_ids 设为启用的系统音色集（克隆音色不受影响）。"""
    store = deps.store()
    count = store.set_voice_pool_enabled(payload.voice_ids)
    return {"enabled": count}


@router.post("/projects/{project_id}/voices/clone")
async def clone_voice(
    project_id: str,
    file: UploadFile = File(...),
    character: str = Form(...),
    voice_name: str = Form(""),
) -> dict[str, Any]:
    """用一段样本音频克隆出这个角色的专属音色。

    「角色音色」之前只能从固定音色池里选——那是空缺：角色要一个只属于他的声音时，
    只能拿真人样本去克隆。这里把样本交给 MiniMax 克隆，拿到新 voice_id 后
    绑到该角色上（与内置音色同一套机制：写进 video_style.voice_casting）。
    """
    store = deps.store()
    project = common.get_project(project_id)
    if not project.raw_text:
        raise HTTPException(400, "这个作品还没有原文")

    sample = await file.read()
    if len(sample) < 1024:
        raise HTTPException(400, "样本音频太小（至少 1KB）")
    if len(sample) > 20 * 1024 * 1024:
        raise HTTPException(400, "样本音频超过 20MB")

    from ..llm.registry import resolve_tts
    from ..llm.tts import build_tts_client

    client = build_tts_client(resolve_tts(get_settings(), store))
    if client is None or not client.available:
        raise HTTPException(503, "没有配置语音合成（在设置页配置后可用），无法克隆音色")

    preview_text = ""
    version = store.latest_version(project)
    if version is not None:
        for sc in version.script.scenes:
            for d in sc.dialogue or []:
                if d.text:
                    preview_text = d.text
                    break
            if preview_text:
                break

    try:
        voice_id = client.clone_voice(
            sample,
            filename=file.filename or "sample.mp3",
            preview_text=preview_text,
        )
    except NotImplementedError as e:
        raise HTTPException(501, str(e)) from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"音色克隆失败：{e}") from None

    name = (voice_name or "").strip() or f"{character} 的专属音色"
    store.save_user_voice(
        voice_id=voice_id,
        name=name,
        project_id=project_id,
        character_id=character,
        sample_name=file.filename or "",
    )

    # 绑到角色上：voice_casting（角色 → voice_id）+ voice_labels（角色 → 显示名）
    style = dict(store.get_video_style(project_id) or {})
    casting = dict(style.get("voice_casting") or {})
    labels = dict(style.get("voice_labels") or {})
    casting[character] = voice_id
    labels[character] = name
    style["voice_casting"] = casting
    style["voice_labels"] = labels
    store.merge_video_style(project_id, style)

    return {
        "voice_id": voice_id,
        "name": name,
        "character": character,
        "detail": "音色已克隆并绑定到该角色。",
    }


@router.get("/projects/{project_id}/voices")
def list_cloned_voices(project_id: str) -> list[dict[str, Any]]:
    """这个作品里克隆出来的音色。"""
    store = deps.store()
    return [
        {
            "voice_id": v.voice_id,
            "name": v.name,
            "character_id": v.character_id,
            "sample_name": v.sample_name,
            "created_at": v.created_at.isoformat() if v.created_at else "",
        }
        for v in store.list_user_voices(project_id)
    ]


@router.post("/projects/{project_id}/library/preview")
def preview_voice(project_id: str, payload: VoicePreview) -> Response:
    """试听：用指定音色读一句样音，返回 mp3 字节流。"""
    common.get_project(project_id)
    from ..llm.registry import resolve_tts
    from ..llm.tts import build_tts_client
    from ..config import get_settings

    client = build_tts_client(resolve_tts(get_settings(), deps.store()))
    if client is None or not client.available:
        raise HTTPException(503, "未配置 TTS（在设置页配置后可用），无法试听")

    text = (payload.text or "").strip()[:80] or SAMPLE_TEXT
    try:
        audio = client.synthesize(text, voice_id=payload.voice_id, speed=1.0)
    except Exception as e:  # noqa: BLE001
        log.warning("试听失败：%s", e)
        raise HTTPException(502, f"试听失败：{e}") from e
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"Cache-Control": "no-store"})
