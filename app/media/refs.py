# =====================================================================
# refs.py —— 参考资产流水线（视觉一致性的图像级锚点）
#
# 工业级思路的落地：先产「角色定妆图 / 场景定妆图」，用视觉模型质检，
# 通过的图注册为全局参考，再回填到每个镜头的 reference_images。
#
# 为什么这样做：文生视频每个镜头独立采样，纯文字锚只能「减少」漂移；
# 把同一个角色的图作为参考喂进去，模型是「看见」同一个人，强度高一个量级。
#
# 流程：定妆 prompt → 文生图 → 视觉质检 →（不合格重生成，最多 N 次）→ 注册
#
# 模型能力本身不在这里实现：文生图与视觉质检都由 app/llm 提供
# （ImageClient / VisionClient），本模块只负责 prompt 组装、质检判定与回填。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

from ..domain import Script, Shot, StyleGuide
from .assets import build_index, rekey
from ..llm import ImageClient, VisionClient

log = logging.getLogger(__name__)

# provider 侧参考图上限（MiniMax 参考图 ≤9）
MAX_REFERENCE_IMAGES = 9


# ---------- 定妆 prompt ----------


def character_prompt(name: str, appearance: str, style: str | None = None) -> str:
    """角色定妆图 prompt：画风决定是「真人剧照」还是「二次元立绘」。

    写实项目的定妆照和漫剧的立绘不是同一张图——用写实的 prompt 生成漫剧角色，
    出来会是「有点动画感的真人」，进视频模型后整片画风就散了。
    """
    from ..pipeline.visual_styles import character_prompt as _style_prompt

    return _style_prompt(name, appearance, style)


def environment_prompt(name: str, description: str, style: str | None = None) -> str:
    """场景定妆图 prompt：固定环境描述 + 空镜约束（不放具体人物，避免把某个人锁进场景）。"""
    from ..pipeline.visual_styles import environment_prompt as _style_prompt

    return _style_prompt(name, description, style)


# ---------- 视觉质检 ----------

_QA_CHARACTER = (
    "你在给 AI 短剧做角色定妆图质检。\n"
    "只有以下三种情况判 FAIL（任一满足即 FAIL）：\n"
    "A) 画面不是「单人 + 正面 + 半身」的定妆照（例如多人合影、全身远景、纯风景）；\n"
    "B) 人物不是中国人 / 东亚面孔。\n"
    "　（例外：若该角色按设定本就戴面具 / 头盔 / 看不到人脸，B 条不适用，不要因此判 FAIL。）\n"
    "C) 画风与要求的「{style}」明显不符——例如要求二维动画赛璐璐上色，画出来的却是"
    "真人照片感 / 写实电影感；或要求写实，画出来是卡通。\n"
    "　（同一大类里的流派差异不算 FAIL：同为二维，线条粗细、上色精致度不同都不拦。）\n"
    "注意：平台水印、背景颜色、服装细节的细微出入都**不算** FAIL，不要因此判不合格。\n"
    "参考描述（用于确认没画错角色，不要求逐条吻合）：\n{expect}\n\n"
    "倒数第二行用一句话说明依据，最后一行只输出一个词：PASS 或 FAIL。"
)

_QA_ENVIRONMENT = (
    "你在给 AI 短剧做场景定妆图质检。\n"
    "只有以下两种情况判 FAIL（任一不满足即 FAIL）：\n"
    "A) 画面主体不是一个「空间环境」（例如是人物特写 / 肖像）；\n"
    "B) 画面里有明显的人物。\n"
    "注意：平台水印、家具数量、灯光位置等细节出入都**不算** FAIL，只要整体空间类型与气质对得上即可。\n"
    "参考描述：\n{expect}\n\n"
    "倒数第二行用一句话说明依据，最后一行只输出一个词：PASS 或 FAIL。"
)


def _verdict(text: str) -> tuple[bool, str]:
    """从质检回答里解析 PASS/FAIL；解析不到时保守判为通过（避免无限重生）。

    只看**最后一行**：回答现在是「一行依据 + 一行判定」，拿全文找子串的话，
    依据里出现「fail / 不合格」字样会把 PASS 翻成 FAIL（反之亦然）。
    """
    lines = [ln.strip() for ln in str(text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return True, "（回答为空，按通过处理）"
    tail = lines[-1].upper()
    reason = "；".join(lines[:-1])[-200:] or tail
    if "FAIL" in tail:
        return False, reason
    if "PASS" in tail:
        return True, reason
    # 最后一行不是判定词（模型没按格式来）：全文兜底扫一次，仍认不出来就放行
    joined = " ".join(lines).upper()
    if "FAIL" in joined:
        return False, reason
    return True, "（未能解析判定，按通过处理）"


# ---------- 生成 + 注册 ----------


def build_reference_assets(
    script: Script,
    style_guide: StyleGuide | None,
    *,
    image_client: ImageClient | None = None,
    vision_client: VisionClient | None = None,
    max_retry: int = 2,
    on_progress: Any = None,
    notes: dict[str, str] | None = None,
) -> dict[str, str]:
    """为角色与场景生成定妆图并质检，返回 {名称: 图片URL}。

    - 角色名 / 地点名与 StyleGuide 的键保持一致，方便回填时查表；
    - 质检不合格就重生成，最多 max_retry 次；仍不合格则保留最后一张（带警告）；
    - 不传客户端时从 app/llm 解析当前配置（.env / DB 里的 provider）。
    """
    provider = image_client
    vision = vision_client
    if provider is None or vision is None:
        from ..deps import llm as get_llm

        facade = get_llm()
        provider = provider or facade.image()
        vision = vision or facade.vision()
    if provider is None or not provider.available:
        raise RuntimeError(
            "未配置文生图 key（IMAGE_PROVIDER=zhipu/cogview 或 OPENAI_API_KEY），"
            "无法生成参考资产"
        )

    def _emit(msg: str) -> None:
        log.info(msg)
        if on_progress:
            on_progress(msg)

    # 注册表以**实体 id** 为键：角色的名字会被用户改，改一次图就找不到了。
    # 老数据里存的是名字键，rekey 会就地换成 id（解析不出来的原样保留）。
    idx = build_index(script)
    refs: dict[str, str] = {}
    appearances = rekey(dict(getattr(style_guide, "character_appearances", {}) or {}), idx) if style_guide else {}
    environments = rekey(dict(getattr(style_guide, "environment_descriptions", {}) or {}), idx) if style_guide else {}
    # 画风决定出的是「剧照」还是「立绘」：漫剧用写实 prompt 会毁掉整片一致性。
    visual_style = getattr(style_guide, "visual_style", None) if style_guide else None

    # 角色：优先用风格指南的固定外貌；缺失则退化为人物名（prompt 里永远写名字）
    char_ids = [c.id for c in script.characters]
    if appearances:
        char_ids = [cid for cid in char_ids if cid in appearances]
    # 用户写的画面要求（按角色 id 或名字都能命中）追加在锚之后
    extra = _notes_by_entity(notes, idx)
    for cid in char_ids:
        name = idx.display(cid)
        appearance = appearances.get(cid, "")
        add = extra.get(cid, "")
        if add:
            appearance = (appearance + "；" + add).strip("； ")
        prompt = character_prompt(name, appearance, visual_style)
        refs[cid] = _generate_with_qa(
            provider, prompt,
            _QA_CHARACTER.format(expect=appearance or name, style=_style_label(visual_style)),
            kind=f"角色·{name}", vision=vision, max_retry=max_retry, emit=_emit,
            negative_prompt=f"{_style_negative(visual_style)}，{_NEG_COMPOSITION}",
        )

    # 场景：优先用风格指南的环境描述；缺失则退化为地点名
    loc_ids = [loc.id for loc in script.locations]
    if environments:
        loc_ids = [lid for lid in loc_ids if lid in environments]
    for lid in loc_ids:
        name = idx.display(lid)
        desc = environments.get(lid, "")
        add = extra.get(lid, "")
        if add:
            desc = (desc + "；" + add).strip("； ")
        prompt = environment_prompt(name, desc, visual_style)
        refs[lid] = _generate_with_qa(
            provider, prompt, _QA_ENVIRONMENT.format(expect=desc or name),
            kind=f"场景·{name}", vision=vision, max_retry=max_retry, emit=_emit,
            negative_prompt=f"{_style_negative(visual_style)}，{_NEG_EMPTY_PLATE}",
        )

    return refs


# 构图排除项：实测 cogview 对正文里「不要多人/背面」的否定句不敏感，
# 但 negative_prompt 结构化字段有效（2026-11-10 质检 FAIL 依据几乎全是构图跑偏）。
# 否定句写正文会反噬（模型把「不要背面」听成「背面」）——排除项走字段，不走文字。
_NEG_COMPOSITION = "多人，合影，背面，背影，侧背影，远景，全身远景，俯拍"
_NEG_EMPTY_PLATE = "人物，人像，角色，行人"


def _style_negative(visual_style: str | None) -> str:
    """画风的反向词（进 API 的 negative_prompt 字段，不写进正文）。"""
    from ..pipeline.visual_styles import get_visual_style

    return str(get_visual_style(visual_style).get("negative") or "")


def _style_label(visual_style: str | None) -> str:
    """画风的中文名（进质检问题，让视觉模型知道「该长什么样」）。"""
    from ..pipeline.visual_styles import get_visual_style

    return str(get_visual_style(visual_style).get("label") or "原设定画风")


def _notes_by_entity(notes: dict[str, str] | None, idx: Any) -> dict[str, str]:
    """把「名字 → 要求」的备注对齐到实体 id（名字键也要能用）。"""
    out: dict[str, str] = {}
    for key, text in (notes or {}).items():
        t = str(text or "").strip()
        if not t:
            continue
        eid = idx.resolve(str(key)) or str(key)
        out[eid] = t
    return out


def _generate_with_qa(
    provider: ImageClient,
    prompt: str,
    qa_question: str,
    *,
    kind: str,
    vision: VisionClient,
    max_retry: int,
    emit: Any,
    negative_prompt: str = "",
) -> str:
    """生成 → 质检 → 不合格重试；返回最终采用的图片 URL。

    negative_prompt 走厂商的结构化字段（画风反向词）：提示词正文里写
    「不要真人照片」反而会被模型听成「要真人照片」（否定句反噬），
    所以正向描述进 prompt、排除项进 negative_prompt。
    """
    url = ""
    for attempt in range(max_retry + 1):
        url = provider.generate(prompt, negative_prompt=negative_prompt)
        if not vision.available:
            emit(f"[{kind}] 已生成（未配置视觉模型，跳过质检）")
            return url
        try:
            text = vision.ask(url, qa_question)
            ok, reason = _verdict(text)
        except Exception as e:  # noqa: BLE001
            emit(f"[{kind}] 质检调用失败，保留该图：{e}")
            return url
        if ok:
            emit(f"[{kind}] 质检通过（第 {attempt + 1} 次）")
            return url
        emit(f"[{kind}] 质检不合格，重生成（第 {attempt + 1} 次）：{reason}")
    emit(f"[{kind}] 重试 {max_retry} 次仍不合格，保留最后一张")
    return url


# ---------- 回填到镜头 ----------


def apply_reference_images(
    shots: list[Shot],
    refs: dict[str, str],
    script: Script,
) -> int:
    """把注册好的参考图按「场景 → 环境图」「人物 → 角色图」回填到每个镜头。

    返回被填充的镜头数。
    """
    if not refs:
        return 0
    # 注册表可能来自老版本（名字键）或新版本（id 键）：先统一成 id 键再查。
    idx = build_index(script)
    refs = rekey(refs, idx)
    scene_map = {sc.id: sc for sc in script.scenes}

    filled = 0
    for shot in shots:
        sc = scene_map.get(shot.scene_id)
        if sc is None:
            continue
        urls: list[str] = []
        env_url = refs.get(sc.location_id) or refs.get(idx.display(sc.location_id))
        if env_url:
            urls.append(env_url)
        for cid in sc.characters:
            url = refs.get(cid) or refs.get(idx.display(cid))
            if url and url not in urls:
                urls.append(url)
        if urls:
            shot.reference_images = urls[:MAX_REFERENCE_IMAGES]
            filled += 1
    return filled
