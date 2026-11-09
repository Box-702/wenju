# =====================================================================
# library.py —— 资产库（Casting Bible）：角色与场景的唯一事实来源
#
# 解决什么
# --------
# 同一个角色，外貌写在 StyleGuide.character_appearances、定妆图写在
# reference_images、声音靠运行期现算——三份数据分散在不同结构里，还都按
# 「名字」做键。结果是：
#   · 声音配错了没人看得见（男主开口是御姐音，2026-10-31 实测）；
#   · 长得对不对、声音对不对，用户只能等成片出来才知道；
#   · 想改一个角色的音色，没有任何入口。
#
# 这里把三份数据聚成一张表，并把「选角依据」摊开给人看：
#   角色 → 外貌锚 / 定妆图 / 性别（+依据）/ 音色（+依据）/ 台词量 / 出场场次
#   地点 → 环境锚 / 定妆图 / 出场场次
#
# 为什么用 video_style_json 存而不是新建表：它本来就是项目级的风格/资产
# 容器（art_director 写、DP 与出片读），加两个键（voice_casting、
# character_genders）就能形成闭环，不必再造一套主键与迁移。资产是「项目的
# 属性」，不是独立实体。
# =====================================================================

from __future__ import annotations

from typing import Any

from ..domain import Script, normalize_visual_style
from .assets import (
    CHARACTER_GENDERS,
    VOICE_CASTING,
    build_index,
    lookup,
    normalize_style,
)
from .casting import VOICE_LABELS, cast_script

# 资产库在 video_style 里占用的键（写回时只动这几个，其余字段原样保留）。
VOICE_KEY = VOICE_CASTING
GENDER_KEY = CHARACTER_GENDERS


def _speech_stats(script: Script) -> dict[str, dict[str, int]]:
    """每个角色/地点在剧本里的戏份统计：{id: {"lines": 台词字数, "scenes": 场次}}。"""
    stats: dict[str, dict[str, int]] = {}
    for sc in script.scenes:
        for cid in sc.characters or []:
            st = stats.setdefault(cid, {"lines": 0, "scenes": 0})
            st["scenes"] += 1
        for b in sc.beats or []:
            if b.type == "dialogue" and b.speaker:
                st = stats.setdefault(b.speaker, {"lines": 0, "scenes": 0})
                st["lines"] += len(b.line or "")
        if sc.location_id:
            loc = stats.setdefault(sc.location_id, {"lines": 0, "scenes": 0})
            loc["scenes"] += 1
    return stats


def build_library(
    script: Script,
    style: dict[str, Any] | None = None,
    *,
    visual_style: str | None = None,
) -> dict[str, Any]:
    """聚合出资产库视图（角色 + 地点 + 汇总告警）。

    ``style`` 是项目 video_style（StyleGuide 的 JSON 形态）；缺字段时按空处理。
    """
    style = style or {}
    # 老项目的 video_style 里没有 visual_style 字段：按实拍处理，不要返回空串
    # （前端拿到空串会显示成「未设定」，让人以为资产库坏了）。
    visual_style = normalize_visual_style(
        visual_style if visual_style is not None else style.get("visual_style")
    )
    # 五份资产统一成「按实体 id 索引」再往下走：老项目存的是名字键，这里顺手换掉。
    style = normalize_style(style, script)
    idx = build_index(script)
    appearances = dict(style.get("character_appearances") or {})
    environments = dict(style.get("environment_descriptions") or {})
    images = dict(style.get("reference_images") or {})
    casting = dict(style.get(VOICE_KEY) or {})
    genders = dict(style.get(GENDER_KEY) or {})
    # 用户克隆出来的音色没有内置名字，显示自己起的那个
    voice_labels = dict(style.get("voice_labels") or {})
    # 用户写的画面要求 / 声音描述
    art_notes, voice_notes = user_notes(style)

    cast = cast_script(
        script, casting=casting, genders=genders,
        appearances=appearances, images=images,
        visual_style=visual_style,
    )
    stats = _speech_stats(script)

    characters: list[dict[str, Any]] = []
    for m in cast.members:
        c = next((x for x in script.characters if x.id == m.char_id), None)
        st = stats.get(m.char_id, {"lines": 0, "scenes": 0})
        item = {
            **m.to_dict(),
            "role": (c.role if c else None) or "other",
            "speech_chars": st["lines"],
            "scenes": st["scenes"],
            "confirmed": bool(m.char_id and m.char_id in casting),   # 音色是否经人工确认
            "has_image": bool(m.image),
        }
        # 克隆音色的显示名（内置音色由 VOICE_LABELS 提供，这里只覆盖自定义的）
        label = voice_labels.get(m.char_id) if m.char_id else ""
        if label:
            item["voice_label"] = label
            item["voice_custom"] = True
        if m.char_id:
            item["note"] = art_notes.get(m.char_id, "")
            item["voice_note"] = voice_notes.get(m.char_id, "")
        characters.append(item)
    # 台词量降序，方便一眼看出谁是主要角色（资产库是给人看的，不是给程序）。
    characters.sort(key=lambda x: (-x["speech_chars"], x["name"]))

    locations: list[dict[str, Any]] = []
    for loc in script.locations:
        st = stats.get(loc.id, {"lines": 0, "scenes": 0})
        locations.append({
            "loc_id": loc.id,
            "name": loc.name,
            "description": lookup(environments, loc.id, idx) or (loc.description or ""),
            "image": lookup(images, loc.id, idx),
            "scenes": st["scenes"],
            "has_image": bool(lookup(images, loc.id, idx)),
            "note": art_notes.get(loc.id, ""),
        })
    locations.sort(key=lambda x: (-x["scenes"], x["name"]))

    warnings: list[str] = list(cast.warnings)
    no_img = [c["name"] for c in characters if not c["has_image"]]
    if no_img:
        warnings.append(f"以下角色还没有定妆图，出片时无法做人物一致性锚定：{'、'.join(no_img)}")
    no_env = [l["name"] for l in locations if not l["has_image"]]
    if no_env:
        warnings.append(f"以下场景还没有场景图：{'、'.join(no_env)}")

    return {
        "project_title": script.title,
        "visual_style": visual_style,
        "characters": characters,
        "locations": locations,
        "voice_options": voice_options(visual_style),
        "warnings": warnings,
    }


def voice_options(visual_style: str | None = None) -> list[dict[str, str]]:
    """可选的音色清单（前端下拉用），漫剧把年轻音色排在前面。"""
    from .casting import voice_pools

    male_pool, female_pool = voice_pools(visual_style)
    out: list[dict[str, str]] = []
    for gender, pool in (("male", male_pool), ("female", female_pool)):
        for v in pool:
            out.append({
                "voice_id": v,
                "label": VOICE_LABELS.get(v, v),
                "gender": gender,
            })
    return out


def user_notes(style: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
    """取用户写的两份要求：画面要求（asset_notes）与声音描述（voice_notes）。

    键是**实体 id**（跟其它资产一样按 id 索引，改名不会丢）。
    """
    st = style or {}
    art = {str(k): str(v) for k, v in (st.get("asset_notes") or {}).items() if str(v).strip()}
    voice = {str(k): str(v) for k, v in (st.get("voice_notes") or {}).items() if str(v).strip()}
    return art, voice


def apply_library_patch(
    style: dict[str, Any],
    patch: dict[str, Any],
    script: Script | None = None,
) -> dict[str, Any]:
    """把资产库的人工修改合并进 video_style（不覆盖未提及的字段）。

    patch 形如::

        {"characters": {"孔乙己": {"gender": "male", "voice_id": "male-qn-badao",
                                   "appearance": "……"}},
         "locations":  {"咸亨酒店": {"description": "……", "image": "https://…"}}}

    ``voice_id`` 为空串表示「撤销人工指定，回到自动选角」。
    """
    # 进来先整体规整成 id 键：这样用户**第一次改资产**时，整份老数据就顺手迁移完了，
    # 之后改名才真正安全（否则只有被改到的那个角色变成 id 键，其余仍是名字键）。
    merged = normalize_style(style, script)
    # patch 的键来自前端（通常是人名，也可能已经是 id）：统一解析成 id 再落库。
    # 解析不出来的（剧本里没有这个名字）原样保留——那是用户的数据，直接丢掉
    # 等于「他说改了、其实没保存」，比留一个孤儿键更糟。
    idx = build_index(script)

    def _key(name: str) -> str:
        return idx.resolve(name) or str(name)

    appearances = dict(merged.get("character_appearances") or {})
    images = dict(merged.get("reference_images") or {})
    casting = dict(merged.get(VOICE_KEY) or {})
    genders = dict(merged.get(GENDER_KEY) or {})
    # 用户写的两份要求
    art_notes = dict(merged.get("asset_notes") or {})
    voice_notes = dict(merged.get("voice_notes") or {})

    for name, change in (patch.get("characters") or {}).items():
        if not isinstance(change, dict):
            continue
        k = _key(name)
        if "gender" in change:
            g = str(change.get("gender") or "").strip().lower()
            if g in ("male", "female"):
                genders[k] = g
            else:
                genders.pop(k, None)
        if "voice_id" in change:
            v = str(change.get("voice_id") or "").strip()
            if v:
                casting[k] = v
            else:
                casting.pop(k, None)
        if "appearance" in change:
            a = str(change.get("appearance") or "").strip()
            if a:
                appearances[k] = a
            else:
                appearances.pop(k, None)
        if "image" in change:
            img = str(change.get("image") or "").strip()
            if img:
                images[k] = img
            else:
                images.pop(k, None)
        if "note" in change:
            n = str(change.get("note") or "").strip()
            if n:
                art_notes[k] = n
            else:
                art_notes.pop(k, None)
        if "voice_note" in change:
            n = str(change.get("voice_note") or "").strip()
            if n:
                voice_notes[k] = n
            else:
                voice_notes.pop(k, None)

    descs = dict(merged.get("environment_descriptions") or {})
    for name, change in (patch.get("locations") or {}).items():
        if not isinstance(change, dict):
            continue
        k = _key(name)
        if "description" in change:
            d = str(change.get("description") or "").strip()
            if d:
                descs[k] = d
            else:
                descs.pop(k, None)
        if "image" in change:
            img = str(change.get("image") or "").strip()
            if img:
                images[k] = img
            else:
                images.pop(k, None)

    merged["environment_descriptions"] = descs

    merged["character_appearances"] = appearances
    merged["reference_images"] = images
    merged[VOICE_KEY] = casting
    merged[GENDER_KEY] = genders
    merged["asset_notes"] = art_notes
    merged["voice_notes"] = voice_notes
    return merged
