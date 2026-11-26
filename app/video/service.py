# =====================================================================
# service.py —— 出片提交服务层（2026-11-11 从 app/api/video.py 下沉）
#
# 为什么下沉：提交幂等（claim）、意图指纹、参考回填、批次推进是**业务规则**，
# 此前住在路由文件里，导致 video/sweeper、chat/tools/video、main 被迫反向
# import api 层（违反 ARCHITECTURE 的单向分层）。现在 api/video.py 只剩
# 薄路由转发，sweeper / 对话工具 / 质检接线都从这里取。
#
# 约定：
#   - 依赖只走 app.deps（全项目唯一装配点），不碰 api 层；
#   - 抛 fastapi.HTTPException 是刻意保留的既有契约：路由直接透传，
#     对话工具侧 catch Exception 读 .detail 转成用户可读文本；
#   - 请求模型（VideoJobCreate / BatchCreateRequest）随业务住在这里，
#     api/schemas.py 再导出保持旧 import 路径不破。
# =====================================================================

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .. import deps
from .base import DEFAULT_ASPECT, default_resolution

log = logging.getLogger(__name__)

# 音色样音的兜底文本：角色没有台词时用它（中性句子，不带情绪）。
NEUTRAL_VOICE_SAMPLE = "这是一段用于音色参考的样音。"

# MiniMax/方舟参考音频的**总时长**上限 15 秒（2026-11-25 实测：单段限制之外
# 还有合计限制，13s + 2.4s 直接被 2013 拒收）。中文 TTS 约 4.5 字/秒，
# 按单元说话人数分预算，留余量。
VOICE_SAMPLE_TOTAL_MS = 15000
VOICE_SAMPLE_CHARS_BY_SPEAKERS = {1: 40, 2: 26}  # 3 人起用下限


def _sample_char_budget(n_speakers: int) -> int:
    """按单元说话人数给单条样音的字符预算（同单元合计不超过总时长上限）。"""
    if n_speakers <= 1:
        return VOICE_SAMPLE_CHARS_BY_SPEAKERS[1]
    if n_speakers == 2:
        return VOICE_SAMPLE_CHARS_BY_SPEAKERS[2]
    return 16  # 3 人：每人约 3.5 秒，合计约 10.5 秒


def _now() -> datetime:
    return datetime.now(UTC)


# ---------- 请求模型（视频域） ----------


class VideoJobCreate(BaseModel):
    """视频生成任务。

    除文本 prompt 外，开放多模态输入入口（对应 MiniMax H3 等
    多模态模型的 content 数组）：首帧图 / 尾帧图 / 参考图 /
    参考音频，以及原生 content 数组直通。

    参考视频**不在这里传**：它由镜头自己带的「参考素材」决定
    （见本模块 _auto_references）。对外不再留
    「手填一条公网 URL 当参考视频」的口子——服务商只认 http(s) 或
    data:base64，而本机回放地址它拉不到（见 app/media/inline.py）。
    """

    shot_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt: str = Field(default="", max_length=8000)  # 纯图 / 视频驱动时可为空
    params: dict[str, Any] = Field(default_factory=dict)
    version_id: str | None = None
    # ---- 多模态入口 ----
    first_frame_image: str | None = None       # 首帧图 URL（图生视频）
    last_frame_image: str | None = None        # 尾帧图（首尾帧控制）
    reference_images: list[str] = Field(default_factory=list)  # 参考图（≤9）
    reference_audios: list[str] = Field(default_factory=list)  # 参考音频（≤3）
    content: list[dict[str, Any]] | None = None  # 原生多模态数组直通（优先级最高）


class BatchCreateRequest(BaseModel):
    """批量生成请求。mode=auto 一次跑完；mode=approval 每镜等用户预览放行。

    resolution 不传时用全局默认（VIDEO_DEFAULT_RESOLUTION）；显式传入则覆盖。
    aspect_ratio 不传时跟随全局默认（竖屏 9:16，短剧形态）；显式传入则覆盖。
    """

    mode: str = Field(default="auto", pattern=r"^(auto|approval)$")
    provider: str | None = None
    model: str | None = None
    aspect_ratio: str | None = Field(default=None, max_length=10)  # 画幅：9:16 / 16:9 / 1:1 等
    version_id: str | None = None
    resolution: str | None = Field(default=None, max_length=20)
    #: 只提交这一集的镜头（分集视频总览的「批量出片」）；不传 = 全片。
    episode_id: str | None = Field(default=None, max_length=80)


# ---------- Provider 解析与任务投递 ----------

#: 方舟（Seedance 系）成片直链的官方域。信任传递只认这些域——判定用解析出的
#: hostname 做精确/后缀匹配，**不做子串匹配**（子串匹配会把 evil-volces.com
#: 这类相似域放进来）。
_ARK_TRUSTED_HOST = "volces.com"
_ARK_TRUSTED_SUFFIX = ".volces.com"


def _is_trusted_relay(url: str, provider_name: str = "",
                      provenance: dict[str, str] | None = None) -> bool:
    """这条接力直链是否是「服务商自家产物、原样回喂可走信任传递」的。

    目前只有方舟（Seedance 系）有此机制且实测有效（30 天内本账号模型产物
    免审）。判据按可靠度排序：
      1. URL 解析出的 hostname 是方舟官方域（volces.com 或其子域）；
      2. 产物 provenance：接力锚来自上一镜成片，哪条 URL 由哪家任务生成
         记录在 relay_providers 里（见 _auto_references）——seedance 产物
         的直链原样回喂才谈得上信任传递；
      3. 兜底：老任务落库时还没有 provenance 字段，沿用旧口径按本次提交的
         provider 名判断。
    都不满足一律走内联老路，MiniMax 行为逐字节不变。
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host == _ARK_TRUSTED_HOST or host.endswith(_ARK_TRUSTED_SUFFIX):
        return True
    if provenance is not None and url in provenance:
        return (provenance[url] or "").strip().lower() == "seedance"
    return (provider_name or "").strip().lower() == "seedance"


def _job_params(payload: dict[str, Any], provider_name: str = "") -> Any:
    """把落库的 params 映射成 Provider 统一入参（多余键进 extra）。

    参考素材是本机文件（回放地址 /api/assets/file/xxx），服务商拉不到这种相对
    地址（实测 MiniMax 判 invalid param），所以在这里统一内联成 data URI。

    接力参考视频的计费裁剪（2026-11-12 账单复盘）：MiniMax 按
    input_seconds + output_seconds 同价计费，整条 4-6s 成片当接力锚比输出
    还贵（实测 ¥8.3/镜里 ¥4.5 是输入视频）。衔接只需要前镜**结尾**，
    公网直链在这里裁尾 2.5s 内联；用户手挑的参考素材不动（那是创作输入，
    不是接力锚）。

    接力锚首选尾帧**图**（2026-11-13 seedance 借鉴清单 #1）：reference_image
    前 5 张免费、零输入秒计费，且静帧只锚身份/环境/光线，不夹带上一镜的
    运镜信号（两条不同机位的接力视频会互相打架，果园 v2「中段偏移」嫌疑）；
    抽帧失败回退裁尾视频（可用性 > 省钱）。

    **例外：方舟（seedance）直链原样透传，禁止转 data URI（2026-11-14 实测）**。
    方舟的真人审核认素材**出处**：本账号 Seedance 成片直链走「信任传递」免审
    （探针 cgt-20261114000257 实锤），但本地抽帧/裁尾再转 base64 后出处信息
    全丢，与随手上传的真人素材无异——必拦。此前「接力含脸概率被拦」的
    结论（5.45）实为本函数把 Ark 直链转成了 data URI：抽帧成功=被拒、
    抽帧失败回退透传=通过，「概率」是假象。信任判据三选一（收紧后）：
    URL hostname 精确命中方舟官方域 > 产物 provenance（relay_providers
    记录该 URL 由哪家任务生成）> 老任务兜底按 provider 名；都不满足走内联。
    """
    from ..media.inline import clip_last_frame_uri, clip_tail_data_uri, inline_media_urls
    from .base import VideoJobParams

    # 这些键有专门的归处：既不要重复塞进 extra，也不要当作未知字段透传给 provider。
    handled_keys = {
        "duration_sec", "resolution", "ratio", "aspect_ratio", "fps", "seed", "negative_prompt",
        "first_frame_image", "last_frame_image",
        "reference_images", "reference_videos", "reference_audios", "content",
        "relay_providers",
    }
    ref_videos: list[str] = []
    relay_frames: list[str] = []
    relay_prov = payload.get("relay_providers") or {}
    for u in inline_media_urls(deps.store(), payload.get("reference_videos") or []):
        if u.startswith("http"):
            if _is_trusted_relay(u, provider_name, relay_prov):
                # 服务商自家成片直链：透传保出处（审核信任 + 免重复流量）
                ref_videos.append(u)
                continue
            # 其他公网直链：优先抽尾帧图（免费档）；失败裁尾内联，再失败透传整条
            frame = clip_last_frame_uri(u)
            if frame:
                relay_frames.append(frame)
            else:
                ref_videos.append(clip_tail_data_uri(u) or u)
        else:
            ref_videos.append(u)
    ref_images = inline_media_urls(deps.store(), payload.get("reference_images") or [])
    # 尾帧图排在定妆图之后：身份锚优先，空间衔接其次；总数不越服务商上限（图 ≤9）
    ref_images = list(dict.fromkeys(ref_images + relay_frames))[:9]
    return VideoJobParams(
        duration_sec=int(payload.get("duration_sec") or 6),
        resolution=str(payload.get("resolution") or "1280x720"),
        aspect_ratio=str(payload.get("ratio") or payload.get("aspect_ratio") or DEFAULT_ASPECT),
        fps=int(payload.get("fps") or 24),
        seed=payload.get("seed"),
        negative_prompt=payload.get("negative_prompt"),
        image_url=payload.get("first_frame_image"),
        last_frame_image=payload.get("last_frame_image"),
        reference_images=ref_images,
        reference_videos=ref_videos,
        reference_audios=inline_media_urls(deps.store(), payload.get("reference_audios") or []),
        content=payload.get("content"),
        extra={k: v for k, v in payload.items() if k not in handled_keys},
    )


def _resolve_video_provider(name: str) -> Any:
    """按 DB 配置解析视频 Provider 实例（缺省回落到环境变量）。"""
    from . import get_registry

    row = next(
        (r for r in deps.store().list_api_providers(kind="video") if r.name == name),
        None,
    )
    kwargs: dict[str, Any] = {}
    if row:
        kwargs = {"api_key": row.api_key, "base_url": row.base_url}
        # 用户自选模型存在 config_json 里；不配则由 provider 用默认模型。
        if row.config.get("model"):
            kwargs["model"] = row.config["model"]
    return get_registry().get_provider(name, **kwargs)


def _submit_video_job(job: Any) -> None:
    """把数据库里的视频任务真正投递到异步生成队列（提交 → 轮询 → 回调写回）。"""
    provider = _resolve_video_provider(job.provider)
    if provider is None:
        raise HTTPException(400, f"未找到视频 Provider: {job.provider}")

    deps.video_manager().submit(job.id, provider, job.prompt,
                                _job_params(dict(job.params or {}), job.provider))


# ---------- 出片意图（claim 幂等） ----------


#: 参与出片意图指纹的参数键。刻意**不含**自动回填的 reference_videos——它是接力锚，
#: 随「上一镜出没出完」变化，拿它算指纹会让批次重投永远判成「意图不同」，幂等等于没做；
#: 用户手挑的那部分单独进意图（见 submit_video_job 里的 manual_reference_videos）。
_INTENT_PARAM_KEYS = (
    "duration_sec",
    "resolution",
    "aspect_ratio",
    "first_frame_image",
    "last_frame_image",
    "reference_images",
    "reference_audios",
    "content",
)


def _video_intent(
    payload: VideoJobCreate, effective_params: dict[str, Any], manual_reference_videos: list[str]
) -> dict[str, Any]:
    """一次出片请求的「意图」——claim key 由它算出。

    两个归一化保证「等价参数 = 同一意图」：
    - 用回填后的 effective_params（时长/分辨率都定了型，「没传」和「传了默认值」同 key）；
    - 空串 / 空列表按「没提」处理，不进指纹。
    """
    params: dict[str, Any] = {}
    for key in _INTENT_PARAM_KEYS:
        value = effective_params.get(key)
        if value:
            params[key] = value
    if manual_reference_videos:
        params["reference_videos"] = list(manual_reference_videos)
    return {
        "prompt": payload.prompt,
        "provider": payload.provider,
        "model": payload.model,
        "version_id": payload.version_id,
        "params": params,
    }


def _intent_params_equal(stored: dict[str, Any], effective: dict[str, Any]) -> bool:
    """_INTENT_PARAM_KEYS 上「存下的 vs 这次要投的」等值比对（空值归一）。"""

    def _norm(value: Any) -> Any:
        return value if value else None

    for key in _INTENT_PARAM_KEYS:
        s, e = _norm(stored.get(key)), _norm(effective.get(key))
        if s == e:
            continue
        # 音色锚的上线豁免（2026-11-20）：回填功能上线**之前**提交的任务，
        # params 里根本没有这个键——比对侧现在算得出来，不等于当年投出去的
        # 不一样。老任务一律认同意图，否则每次重开批次都会把上线前的成功
        # 镜头重新烧一遍（《替身v2》16 镜 ≈ ¥85 的实弹就埋在这）。
        # 存过（非空）而两边不同 = 真换过音色，判成新意图。
        if key == "reference_audios" and s is None:
            continue
        return False
    return True


def _same_generation_intent(
    job: Any, *, prompt: str, provider: str, model: str,
    effective_params: dict[str, Any], manual_reference_videos: list[str],
) -> bool:
    """「这条历史任务」与「这次要投的」是不是同一件事——单镜与批量共用一把尺。

    比对口径：prompt、provider、model、_INTENT_PARAM_KEYS（时长/分辨率/画幅/
    首尾帧/参考图/参考音频/content）+ 用户手挑的参考视频。自动接力锚不参与
    （随出片进度变化，见 _INTENT_PARAM_KEYS 注释）。
    """
    if (job.prompt or "").strip() != (prompt or "").strip():
        return False
    if (job.provider or "") != (provider or "") or (job.model or "") != (model or ""):
        return False
    stored = dict(job.params or {})
    if not _intent_params_equal(stored, effective_params):
        return False
    # 参考视频：存下来的是「手挑 + 自动接力锚」合并结果，接力锚随出片进度变化不比，
    # 只按长度截出手挑那段比对。
    if manual_reference_videos:
        stored_refs = list(stored.get("reference_videos") or [])
        if stored_refs[: len(manual_reference_videos)] != manual_reference_videos:
            return False
    return True


def _same_intent(job: Any, payload: VideoJobCreate, effective_params: dict[str, Any],
                 manual_reference_videos: list[str]) -> bool:
    """在途任务与新请求是不是「同一件事」（claim 之前的前置比对）。

    claim 表只覆盖**从 submit_video_job 进来**的轮；成片质检的自动重 roll
    （app/video/qa.py）直接 create_video_job、不占 claim。没有这层比对的话：
    质检刚重 roll 出一个在途任务，用户又说「出片」——claim 查不到、任务又不是
    自己提交的，就会第三次建档、同一个镜头两条任务并行烧钱。
    """
    return _same_generation_intent(
        job, prompt=payload.prompt, provider=payload.provider, model=payload.model,
        effective_params=effective_params,
        manual_reference_videos=manual_reference_videos,
    )


# ---------- 提交（单镜 / 镜头组，S2 PLAN-shot-group） ----------


def generation_units(shots: list[Any]) -> list[dict[str, Any]]:
    """把镜头列表折叠成生成单元：单镜自成单元；同 group_id 的机位合成一个单元。

    组键是 **(scene_id, group_id)**，不是裸 group_id：两个场景复用同一个
    group_id 时绝不能并成一个单元（旧写法全局按 group_id 分桶，跨场重复键
    会把两段不相邻的戏拼成一次生成、时长爆表、成员互相污染）。

    单元 = 一次视频生成。组单元的 prompt 取组头 group_prompt（DP 折叠产物，
    缺则回落组头 video_prompt），时长 = 组内各机位之和；job 的 shot_id 用
    组头 id，成员 id 列表进 params（finalize/配音按成员展开，S2b）。
    旧数据（无 group 字段）逐字节等价于「每镜一单元」——兼容是设计出来的。
    """
    by_gid: dict[tuple[str, str], list[dict[str, Any]]] = {}
    units: list[dict[str, Any]] = []
    for s in shots:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        gid = str(s.get("group_id") or "")
        if gid:
            by_gid.setdefault((str(s.get("scene_id") or ""), gid), []).append(s)
        else:
            units.append(_unit_of([s]))
    for members in by_gid.values():
        members.sort(key=lambda x: (int(x.get("group_index") or 0),
                                    int(x.get("order") or 0)))
        units.append(_unit_of(members))
    units.sort(key=lambda u: u["head_order"])
    return units


def unit_prompt(shot: dict[str, Any]) -> str:
    """这一镜真正送去生成的提示词。

    组头持有**折叠后的组提示词**（group_prompt）——那是把组内几个机位连成
    一次生成、一条视频里切几个镜头的那句话，也就是最终提交给视频模型的
    原文。组头的 video_prompt 只是它自己那一机位的单镜版本，**不会**被提交。
    单镜（无 group_prompt）用 video_prompt。

    这个口径必须处处一致：等值校验、提交工具、前端展示与编辑全部走它。
    曾经各处各取各的（工具层传 video_prompt、校验层比 group_prompt），
    结果「指定镜头出片」在有镜头组的作品上必然 409——用户看到并批准的是
    一条文本，系统提交的却是另一条。
    """
    return str(shot.get("group_prompt") or "") or str(shot.get("video_prompt") or "")


def _unit_of(members: list[dict[str, Any]]) -> dict[str, Any]:
    head = members[0]
    prompt = unit_prompt(head) if len(members) > 1 else ""
    prompt = prompt or str(head.get("video_prompt") or "")
    return {
        "shot_id": str(head.get("id")),
        "group_id": str(head.get("group_id") or "") if len(members) > 1 else "",
        "scene_id": str(head.get("scene_id") or ""),
        "member_ids": [str(m.get("id")) for m in members],
        "prompt": prompt,
        "duration_sec": sum(max(0.0, float(m.get("duration_sec") or 0)) for m in members),
        "head_order": int(head.get("order") or 0),
    }


def unit_approved(all_shots: list[Any], unit: dict[str, Any]) -> bool:
    """这个生成单元能不能提交。

    规则：组头已批准 **且** 组内没有机位被标「需修改」。
    - 组头承载的是折叠后的组提示词，用户批准它 = 批准这一整段戏；
      组内机位不再各自要批（否则一段戏要批 N 次，用户会以为出片坏了）。
    - 但只要有任何一个机位被标了 needs_revision，就不投：批了半段戏就提交
      等于替用户做主（那句「需修改」就是他明确说了这段还不行）。
    """
    member_ids = set(unit.get("member_ids") or [])
    members = [s for s in all_shots
               if isinstance(s, dict) and s.get("id") in member_ids]
    head = next((s for s in members if s.get("id") == unit["shot_id"]), None)
    if not head or head.get("prompt_status") != "approved":
        return False
    return not any(m.get("prompt_status") == "needs_revision" for m in members)


def submit_video_job(project_id: str, payload: VideoJobCreate) -> tuple[Any, bool]:
    """创建视频任务并投递（单镜提交与批量批次共用的入口）。

    返回 (任务, 是否新建)。**幂等由 claim 表保证**（`VideoJobClaim`，见 app/store.py）：
    同一镜头 + 同一意图在途期间，第二次请求命中既有 claim——
    任务已建好就直接复用它（不新建、不重复计费）；正卡在「claim 了但还没建完任务」
    的缝里就报 409 停止，绝不把同一个意图投出去两次。
    换参数的重出片是不同意图，正常新建。

    两条边界：
    - 上一版已出片的镜头再次提交：任务进终态时 claim 自动释放，属合法重出片。
    - 进程崩溃留下的空 claim 由 10 分钟超时自愈；残留的终态 claim 就地清理。

    含提交时自动锚回填（定妆图 + 前镜成片接力）与默认分辨率策略：
    payload.params 未显式带 resolution 时落全局默认（VIDEO_DEFAULT_RESOLUTION）。

    时长优先级：调用方显式指定 > 导演给该镜头的 duration_sec > 节奏默认值。
    这里曾经写死 `params.get("duration_sec") or 6`，导演设计的 8–10 秒镜头
    一律被压成 6 秒送进模型——动作被硬塞进更短的时间，只能糊成一团。
    """
    from .rhythm import suggest_duration

    store = deps.store()
    # ---- 出片闸门（fail-closed）----
    # 此前的写法是「解析不到镜头就跳过校验」：传一个不属于本作品/不存在的
    # version_id 或 shot_id，闸门整段被绕开，未批准的 Prompt 能直接提交付费生成。
    # 现在反过来：解析不到 = 拒绝；提交内容与已批准的 Prompt 不一致 = 拒绝。
    # 所有合法调用方（UI / 对话工具 / 批次）传的都是该镜头当前存的那条 Prompt，
    # 等值校验只挡「换一条没批过的 Prompt 硬塞进来」这一种越权路径。
    if not payload.version_id:
        raise HTTPException(400, "提交出片必须指定镜头方案版本（version_id）")
    version = store.get_video_version(payload.version_id)
    if version is None or version.project_id != project_id:
        raise HTTPException(404, "镜头方案不属于该作品或不存在，无法提交出片")
    shot = next(
        (s for s in (version.shots or [])
         if isinstance(s, dict) and s.get("id") == payload.shot_id),
        None,
    )
    if shot is None:
        raise HTTPException(404, f"镜头 {payload.shot_id} 不在该方案里，无法提交出片")
    # ---- 镜头组感知（S2，PLAN-shot-group）----
    # 提交单元可以是组：payload.shot_id 是组头，等值校验对 group_prompt，
    # 时长对组内各机位之和。单镜（无 group_id）走原路径，逐字节不变。
    gid = str(shot.get("group_id") or "")
    # 组成员必须同场：group_id 只在场景内唯一（两个场景复用同名组键时，
    # 跨场合并会把不相邻的戏拼成一次生成）。
    members = [s for s in (version.shots or [])
               if isinstance(s, dict) and str(s.get("group_id") or "") == gid
               and str(s.get("scene_id") or "") == str(shot.get("scene_id") or "")] \
        if gid else [shot]
    # 组头的可提交提示词是折叠后的组提示词（unit_prompt 单一口径）；
    # 单镜走自己的 video_prompt。
    approved_prompt = unit_prompt(shot)
    if not approved_prompt:
        raise HTTPException(409, "该镜头还没有 Prompt，无法提交出片")
    if shot.get("prompt_status") != "approved":
        raise HTTPException(409, "该镜头的 Prompt 尚未批准，请先完成人工审阅")
    if (payload.prompt or "").strip() != approved_prompt.strip():
        raise HTTPException(409, "提交内容与该镜头已批准的 Prompt 不一致——改过内容需要重新批准")
    # 组内有机位被标「需修改」就不投这一段（与批次同一口径，见 unit_approved）：
    # 提交的是折叠后的组提示词，它覆盖组内全部机位——批了半段戏就提交
    # 等于替用户做主。
    if gid and len(members) > 1:
        blocked = [str(m.get("id")) for m in members
                   if m.get("prompt_status") == "needs_revision"]
        if blocked:
            raise HTTPException(
                409,
                f"这一段里有机位被标记为需要修改（{'、'.join(blocked)}），"
                "先改完再出片——提交的提示词覆盖整段，半段没批不该发出去。",
            )

    # 时长：显式 > 导演（组=各机位之和）> 节奏默认（按本场 pacing + 项目改编类型给的建议值）。
    # 短剧项目默认 4–5 秒一镜——短视频时代的注意力只给开头三秒，不能用电影的
    # 8 秒基准（2026-10-31 用户反馈）。
    explicit = payload.params.get("duration_sec")
    director_sec = sum(float(m.get("duration_sec") or 0) for m in members)
    pacing = getattr(version, "pacing", None) if version else None
    project = store.get_project(project_id)
    fmt = getattr(project, "adaptation_type", None)
    duration = int(round(explicit or director_sec or suggest_duration(pacing, fmt)))

    stored_params = dict(payload.params)
    # 落库固定下来：重试、批量重投都要用同一个时长，不能再各自回落到默认值。
    stored_params["duration_sec"] = duration
    stored_params.setdefault("resolution", default_resolution())
    # 组任务：成员机位清单随任务落库——finalize/配音按它把一条组视频展开
    # 回各机位的时间轴（S2b），也让 UI 知道这条任务覆盖哪几个镜头。
    if gid and len(members) > 1:
        stored_params["group_member_ids"] = [str(m.get("id")) for m in members]
    for key in ("first_frame_image", "last_frame_image", "reference_images",
                "reference_audios", "content"):
        value = getattr(payload, key)
        if value:
            stored_params[key] = value

    # 先记下用户手挑的参考视频，再补自动锚——回填会把接力锚并进来，混在一起分不清意图。
    manual_reference_videos = list(payload.params.get("reference_videos") or [])
    # 提交时临时挂的参考素材（Agent 在对话里点名要挂的那条，如「用白模当参考
    # 出这一镜」）：与镜头自带的合并。只影响这一次提交，不改镜头方案——
    # 参考素材是**输入**，不是提示词的一部分，不该动已批准的镜头内容。
    extra_ids = [str(x).strip() for x in (payload.params.get("reference_asset_ids") or [])]
    if extra_ids:
        shot_refs = dict(shot)
        shot_refs["reference_asset_ids"] = (
            list(shot.get("reference_asset_ids") or []) + extra_ids
        )
        for url in _manual_reference_videos(project_id, shot_refs):
            if url not in manual_reference_videos:
                manual_reference_videos.append(url)
    try:
        _auto_references(project_id, payload.shot_id, payload.version_id, stored_params,
                         provider=payload.provider)
    except ValueError as e:
        raise HTTPException(409, f"请先确认镜头造型：{e}") from e

    # 用量摘要（D-53）：界面不再展示费用估算——预估与实付脱节只会伤信任
    # （导入回执「预估 ¥3.6」vs 实付 ¥27.7 的教训）。落库三类创作者数量：
    # 生成秒数 / 参考视频条数与总时长 / 参考图张数。放在锚回填**之后**：
    # 接力锚是回填进来的，回填前算的用量必然少报。
    try:
        from .usage import job_usage, relay_durations

        stored_params["usage"] = job_usage(
            stored_params,
            store=store,
            url_durations=relay_durations(store.list_video_jobs(project_id)),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("用量摘要生成失败（不阻断提交）：%s", e)

    # 意图占位（claim）：建任务之前先占——崩溃轮、并发轮、重试轮都有唯一出口。
    # claim 表只覆盖从本函数进来的轮；成片质检的自动重 roll 直接建任务不占 claim，
    # 所以 claim 未命中时还要再查一层「同镜头有在途的同意图任务吗」兜底，
    # 否则质检刚重 roll 完、用户一句「出片」就能让同一镜头三条任务并行。
    key = store.video_claim_key(
        project_id, payload.shot_id,
        _video_intent(payload, stored_params, manual_reference_videos),
    )
    outcome, existing_task_id = store.claim_video_job(
        key, project_id=project_id, shot_id=payload.shot_id
    )
    if outcome == "replay":
        if existing_task_id:
            existing = store.get_video_job(existing_task_id)
            if existing is not None:
                log.info("镜头 %s 同意图任务在途（%s），复用不重复提交", payload.shot_id, existing.id)
                return existing, False
        # 卡在缝里 / 并发建档中：拒绝重放，绝不投第二次。
        raise HTTPException(
            409, "这一镜的出片请求正在提交中（上一轮还没完成建档）。稍等片刻再试，不要重复提交。"
        )
    active = store.find_active_video_job(project_id, payload.shot_id)
    if active is not None and _same_intent(active, payload, stored_params, manual_reference_videos):
        store.release_video_claim(key)  # 让位给真正在跑的那条，claim 不许留着挡路
        log.info("镜头 %s 有在途任务（非本路径提交），同意图直接复用：%s", payload.shot_id, active.id)
        return active, False

    try:
        job = store.create_video_job(
            project_id=project_id,
            shot_id=payload.shot_id,
            provider=payload.provider,
            model=payload.model,
            prompt=payload.prompt,
            params=stored_params,
            version_id=payload.version_id,
            # 费用估算已随 D-53 退出产品链路（usage 取代）；DB 列保留给历史数据。
            cost_estimate=None,
        )
    except Exception:
        store.release_video_claim(key)  # 建档失败：占位要还回去，别挡下一次尝试
        raise
    store.finish_video_claim(key, job.id)
    try:
        _submit_video_job(job)
    except Exception:
        # 投递失败：任务标终态（顺带释放 claim），别让占位挡住用户改好之后的重试。
        try:
            store.update_video_job(job.id, status="failed", error_message="投递失败")
        except Exception:  # noqa: BLE001
            log.warning("投递失败后回写任务 %s 状态失败", job.id)
        raise
    return job, True


# ---------- 参考回填 ----------


def _manual_reference_videos(project_id: str, shot: dict[str, Any]) -> list[str]:
    """镜头里用户挑的参考素材 → 本机回放地址列表。

    存的是素材 id（库里 id 才是真相，名字只是显示用的别名），提交给服务商前
    由 _job_params 内联成 data URI；这里先还原成回放地址，好和自动接力的公网
    直链放在同一个列表里统一排序、截断。
    """
    store = deps.store()
    urls: list[str] = []
    for asset_id in shot.get("reference_asset_ids") or []:
        asset = store.get_asset(str(asset_id))
        if asset is None or asset.project_id != project_id:
            continue
        if asset.url and asset.url not in urls:
            urls.append(asset.url)
    return urls


def _shot_speakers(shot_plan: list[dict[str, Any]], shot_id: str, script: Any) -> list[str]:
    """该提交单元（单镜或整组）里开口说话的角色 id，按出现顺序去重。

    组任务投的是折叠后的组提示词，覆盖全组机位——音色锚要给的是**整组**的
    说话人，不只组头。dialogue_ref 指向剧本节拍，节拍带 speaker。
    """
    shots = [s for s in (shot_plan or []) if isinstance(s, dict)]
    head = next((s for s in shots if s.get("id") == shot_id), {})
    gid = str(head.get("group_id") or "")
    members = (
        [s for s in shots
         if str(s.get("group_id") or "") == gid
         and str(s.get("scene_id") or "") == str(head.get("scene_id") or "")]
        if gid else [head]
    )
    refs = [str(m.get("dialogue_ref") or "").strip() for m in members]
    refs = [r for r in refs if r]
    beat_speaker: dict[str, str] = {}
    for sc in (script.scenes if script else []):
        for b in sc.beats:
            if b.id in refs and b.speaker:
                beat_speaker[b.id] = b.speaker
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        cid = beat_speaker.get(r)
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _character_lines(script: Any, char_id: str, *, max_chars: int = 60) -> str:
    """该角色在剧本里的台词（按顺序拼接，最多约 max_chars 字）。

    音色样音要「像这个角色在说话」：用他/她自己的台词做克隆参考，语气、
    停连、情绪都比中性占位句更像成片。60 字 ≈ 6-8 秒，落在参考音频
    [2,15] 秒区间中部；抓不到台词返回空串（调用方回退中性句）。
    """
    if script is None or not char_id:
        return ""
    parts: list[str] = []
    total = 0
    for sc in (getattr(script, "scenes", None) or []):
        for b in (getattr(sc, "beats", None) or []):
            if getattr(b, "speaker", None) != char_id:
                continue
            line = str(getattr(b, "line", None) or "").strip()
            if not line:
                continue
            if total + len(line) > max_chars and parts:
                break
            parts.append(line)
            total += len(line)
        if total >= max_chars:
            break
    return "。".join(p for p in parts if p)


def _provider_supports_reference_audio(provider: str) -> bool:
    """该厂商是否接受参考音频：按 VideoProvider.supports_reference_audio 能力声明判断。

    以前是按厂商名硬编码 ("seedance", "minimax")——新增厂商时要记得回来改，
    声明的字段却没被读。未知厂商/注册表失败一律按否处理（不阻断出片）。
    """
    name = (provider or "").strip().lower()
    if not name:
        return False
    try:
        from . import get_registry

        prov = get_registry().get_provider(name)
        return bool(getattr(prov, "supports_reference_audio", False))
    except Exception:  # noqa: BLE001
        return False


def _voice_sample(store: Any, project_id: str, char_id: str, voice_id: str,
                  *, text: str = "", create: bool = True) -> str | None:
    """角色音色样音的素材回放地址（有缓存则复用，没有则 TTS 合成一条入库）。

    音色规范（2026-11-20）：native 音画同生模式下，视频模型没有 voice_id 字段，
    唯一能把「该角色用什么嗓子」传进去的通道是 reference_audio——传一段
    该角色音色的样音，模型生成的台词人声会向它靠。样音按 (角色, 音色, 文本)
    缓存，文本没变就绝不重复合成（MiniMax TTS 很便宜，但绝不逐镜重复合成）。

    create=False（比对侧）：只查缓存不合成不写库——幂等比对发生在每个成功任务
    上，绝不能在这里烧 TTS 或改选角表。

    text 给角色自己的台词（2026-11-25 起）：带情绪与停连的台词样本比中性占位句
    更像成片里的嗓子；为空才回退中性句。缓存键带文本摘要，换样本文本自然生成
    新样音，旧样音保留（版本可回溯）。
    任何一步失败都返回 None（不阻断出片）。
    """
    if not voice_id:
        return None
    sample_text = (text or "").strip() or NEUTRAL_VOICE_SAMPLE
    digest = hashlib.sha1(sample_text.encode("utf-8")).hexdigest()[:8]
    tag = f"音色样音::{char_id}::{voice_id}::{digest}"
    legacy_tag = f"音色样音::{char_id}::{voice_id}"
    existing = {a.name: a.url for a in store.list_assets(project_id, kind="audio") if a.url}
    if tag in existing:
        return existing[tag]
    # 兼容：中性占位句时代的老样音继续沿用——不能静默升级，否则老项目重开
    # 批次时「音色变了」会被判成新意图，把已成功的镜头整批重烧一遍。
    # 想升级到台词样本：删掉该角色的旧样音资产，或换音色后重新出片。
    if legacy_tag in existing:
        return existing[legacy_tag]
    if not create:
        return None
    try:
        from .finalize import _resolve_tts

        tts = _resolve_tts(store)
        if tts is None:
            return None
        audio = tts.synthesize(sample_text, voice_id=voice_id)
    except Exception as e:  # noqa: BLE001
        log.warning("音色样音合成失败（忽略）：%s %s", char_id, str(e)[:80])
        return None
    if not audio:
        return None
    from ..store import gen_id

    asset_id = gen_id("asset")
    try:
        store.add_asset(
            project_id, kind="audio", name=tag, source="voice_sample",
            url=f"/api/assets/file/{asset_id}", id=asset_id, content=bytes(audio),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("音色样音入库失败（忽略）：%s", str(e)[:80])
        return None
    return f"/api/assets/file/{asset_id}"


def voice_audios_for(
    store: Any, project_id: str, shot_plan: list[dict[str, Any]], shot_id: str,
    script: Any, *, provider: str = "", stored_params: dict[str, Any] | None = None,
    synthesize: bool = True,
) -> list[str]:
    """该提交单元该带的音色样音（≤3）；不适用时返回空列表。

    音色规范（2026-11-20）：native 音画同生下视频模型没有 voice_id 字段，
    唯一能把「该角色用什么嗓子」传进去的通道是 reference_audio——传该角色
    音色的样音，模型生成的台词人声向它靠（Seedance 2.0 官方：≤3 段、
    单段 2–15s、必须同时带 ≥1 张图或视频——定妆图回填保证了这条）。

    只在满足全部条件时给：① 厂商声明了参考音频能力（VideoProvider.
    supports_reference_audio，MiniMax/方舟为真）；② 调用方没显式给过
    reference_audios（显式优先，不覆盖）；③ 有剧本（说话人要从节拍里解析）。
    任何一步失败返回空——音色锚是锦上添花，绝不阻断出片。

    提交侧与比对侧（_unit_intent）共用这一份口径：回填进了 stored_params 的
    reference_audios 必须能被比对侧重算出来，否则幂等会把「没变的任务」判成
    新意图白烧钱（_INTENT_PARAM_KEYS 含 reference_audios，与参考图同坑同修）。
    """
    if not _provider_supports_reference_audio(provider):
        return []
    if stored_params is not None and stored_params.get("reference_audios"):
        return []
    if script is None or not shot_plan:
        return []
    try:
        if synthesize:
            # 提交侧：自动选角先落库（resolve_casting 纯读拿不到推断结果的持久化），
            # 只在 voice_casting 缺该角色时补，人工指定的不动。
            from ..media.casting import cast_and_persist

            cast = cast_and_persist(store, project_id, script)
        else:
            # 比对侧：纯读——不写选角表、不烧 TTS（幂等比对跑在每个成功任务上）。
            from ..media.casting import resolve_casting

            cast = resolve_casting(store, project_id, script)
        casting_map = {m.char_id: m.voice_id for m in cast.members if m.char_id}
        audios: list[str] = []
        speakers = _shot_speakers(shot_plan, shot_id, script)
        budget = _sample_char_budget(len(speakers))
        for cid in speakers:
            url = _voice_sample(store, project_id, cid, casting_map.get(cid, ""),
                                text=_character_lines(script, cid, max_chars=budget),
                                create=synthesize)
            if url and url not in audios:
                audios.append(url)
        return audios[:3]
    except Exception as e:  # noqa: BLE001
        log.warning("音色锚回填失败（不阻断出片）：%s", str(e)[:120])
        return []


def _auto_references(project_id: str, shot_id: str | None, version_id: str | None, stored_params: dict[str, Any], *, provider: str = "") -> None:
    """提交时自动补齐多模态一致性输入（调用方显式提供的键优先，不覆盖）。

    - reference_videos：连续性接力——按镜头计划（reference_group / chain_from）
      解析「已完成前镜」的成片 URL，锁跨镜风格/光影/人物；
    - reference_images：风格指南注册表里的定妆图（场景→环境图、人物→角色图），
      图像级锚比纯文字锚约束力强一个量级。
    """
    if not shot_id:
        return
    store = deps.store()

    versions = store.list_video_versions(project_id)
    version = next((v for v in versions if v.id == version_id), None)
    if version is None:
        ordered = sorted(versions, key=lambda v: v.created_at)
        version = ordered[-1] if ordered else None
    if version is None or not version.shots:
        return
    if not any(s.get("id") == shot_id for s in version.shots if isinstance(s, dict)):
        return

    jobs = [
        j for j in store.list_video_jobs(project_id)
        if j.status == "succeeded" and j.video_url and j.shot_id
        # 只拦「两个版本都有明确归属且不同代」的成片（果园 v4 排查发现的重号
        # 污染洞）；单镜手提交的任务常不带 version_id，无法证明是别代，放行。
        and not (version_id and j.version_id and j.version_id != version_id)
    ]
    completed_urls = {j.shot_id: j.video_url for j in jobs}
    # 产物 provenance：每条成片 URL 由哪家任务生成。接力锚的信任传递判定
    # （_is_trusted_relay）据此认 URL 的出身，而不是拿「本次提交的 provider 名」猜。
    url_provider: dict[str, str] = {}
    for j in jobs:
        url_provider.setdefault(str(j.video_url), str(getattr(j, "provider", "") or ""))
    style = store.get_video_style(project_id) or {}
    registry = dict(style.get("reference_images") or {})

    script = None
    try:
        project = store.get_project(project_id)
        v = store.latest_version(project) if project else None
        if v is not None:
            # 有剧本版本才解析；import_storyboard 直导的分镜作品本来就没有
            # 剧本版本（果园 demo），latest_version 返回 None 是正常路径，
            # 不该走异常——旧写法把「没有剧本」报成「取剧本失败」，误导排查。
            script = v.script
    except Exception as e:  # noqa: BLE001
        log.warning("自动参考回填：剧本解析失败（%s），跳过定妆图回填", str(e)[:80])

    from .continuity import resolve_submission_references

    refs = resolve_submission_references(
        shot_id=shot_id,
        shot_plan=[s for s in version.shots if isinstance(s, dict)],
        completed_urls=completed_urls,
        image_registry=registry,
        style=style,
        script=script,
    )
    if script is not None and (style.get("character_looks") or style.get("scene_look_ids") or style.get("shot_look_ids")):
        from ..domain import Shot
        from ..media.looks import all_looks, selected_look
        from ..media.refs import apply_reference_images

        raw_head = next((s for s in version.shots if isinstance(s, dict) and s.get("id") == shot_id), {})
        gid = raw_head.get("group_id")
        members = [s for s in version.shots if isinstance(s, dict) and s.get("group_id") == gid
                   and s.get("scene_id") == raw_head.get("scene_id")] if gid else [raw_head]
        plan = [Shot.model_validate(s) for s in version.shots if isinstance(s, dict)]
        scenes = {s.id: s for s in script.scenes}
        looks = all_looks(style, script)
        used: dict[str, str] = {}
        images: list[str] = []
        for raw in members:
            member = Shot.model_validate(raw)
            scene = scenes.get(member.scene_id)
            if scene is None:
                continue
            for cid in scene.characters:
                look = selected_look(character_id=cid, scene=scene, shot=member,
                                     looks=looks, shot_plan=plan, style=style)
                if not look:
                    continue
                if cid in used and used[cid] != look["id"]:
                    raise ValueError(f"镜头组内角色 {cid} 使用不同造型，请拆组后出片")
                used[cid] = look["id"]
            apply_reference_images([member], registry, script, style=style, shot_plan=plan)
            images.extend(member.reference_images)
        refs["reference_images"] = list(dict.fromkeys(images))[:9]
    # 参考视频：用户挑的参考素材（本地文件）+ 自动接力的前镜成片（公网直链）合并。
    # 服务商最多收 3 条（实测超了报「参考视频最多 3 个」），按「显式 > 接力」填满。
    # 这里只存 URL；接力片段的计费裁剪（尾部 2.5s）在投递时由 _job_params 做，
    # 免得 base64 进 DB 把库撑爆（2026-11-12 账单复盘：输入秒数按输出同价计费）。
    shot = next((s for s in version.shots if isinstance(s, dict) and s.get("id") == shot_id), {})
    manual = _manual_reference_videos(project_id, shot)
    merged: list[str] = []
    for url in (
        list(stored_params.get("reference_videos") or [])
        + manual
        + list(refs.get("reference_videos") or [])
    ):
        if url and url not in merged:
            merged.append(url)
    stored_params["reference_videos"] = merged[:3]
    if script is not None and (style.get("character_looks") or style.get("scene_look_ids") or style.get("shot_look_ids")):
        stored_params["reference_images"] = refs.get("reference_images") or []
    elif refs.get("reference_images") and not stored_params.get("reference_images"):
        stored_params["reference_images"] = refs["reference_images"]
    # relay_providers 只记接力锚的出身（本机回放地址的手挑素材不在其中）；
    # 没有出身的 URL 进不了映射，_job_params 侧就拿不到信任传递——
    # 任意公网 URL + provider=seedance 不再被无条件放行。
    stored_params["relay_providers"] = {
        u: url_provider[u] for u in stored_params.get("reference_videos") or []
        if u in url_provider
    }
    # ---- 音色锚回填（2026-11-20 音色规范）----
    audios = voice_audios_for(store, project_id,
                              [s for s in version.shots if isinstance(s, dict)],
                              shot_id, script,
                              provider=provider, stored_params=stored_params)
    if audios:
        stored_params["reference_audios"] = audios


# ---------- 取消 / 重试 ----------


def cancel_video_job(job_id: str) -> dict[str, Any]:
    """取消视频生成任务：先请服务商侧停止，再收尾本地状态。

    返回里带 `remote_cancelled` 与 `note`：**本地置为已取消 ≠ 服务商那边
    真的停了**。MiniMax V2 没有取消端点（见 providers/minimax.py），那条
    任务可能还在生成、费用照计——话必须说到位，否则「已取消」会被用户
    理解成「不花钱了」。
    """
    j = deps.store().get_video_job(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    # 服务商侧取消是尽力而为：任务已不在内存队列（如进程重启过）或 provider
    # 不支持取消时，不影响本地状态收尾——本地状态才是前端看到的那份。
    remote_cancelled = False
    supports_cancel = False
    provider_label = j.provider or "服务商"
    try:
        provider = _resolve_video_provider(j.provider)
        if provider is not None:
            supports_cancel = bool(getattr(provider, "supports_cancel", False))
            provider_label = getattr(provider, "label", None) or provider_label
            remote_cancelled = bool(deps.video_manager().cancel(job_id, provider))
    except Exception as e:  # noqa: BLE001
        log.warning("通知 provider 取消任务 %s 失败：%s", job_id, e)
    deps.store().update_video_job(job_id, status="cancelled", finished_at=_now())
    if remote_cancelled:
        note = "已在服务商侧取消。"
    elif not supports_cancel:
        note = (
            f"已在本地取消，但 {provider_label} 没有可用的取消接口——"
            "服务商那边可能还在生成，费用照计。"
        )
    else:
        note = (
            f"已在本地取消；{provider_label} 侧的取消请求没能确认，"
            "那条任务可能还在生成，费用照计。"
        )
    return {
        "id": job_id,
        "status": "cancelled",
        "remote_cancelled": remote_cancelled,
        "supports_cancel": supports_cancel,
        "note": note,
    }


def retry_video_job(job_id: str) -> dict[str, Any]:
    """重试失败的视频生成任务：重置状态并重新投递到异步队列。"""
    j = deps.store().get_video_job(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    if j.status not in ("failed", "cancelled"):
        raise HTTPException(400, f"任务状态为 {j.status}，只能重试失败或已取消的任务")
    deps.store().update_video_job(
        job_id, status="pending", error_message=None, video_url=None, finished_at=None
    )
    _submit_video_job(deps.store().get_video_job(job_id))
    return {"id": job_id, "status": "pending"}


def reroll_shot(project_id: str, shot_id: str, *, version_id: str | None = None) -> dict[str, Any]:
    """单镜重出片（工作台「重新生成」按钮与修复工具链的后端底座）。

    语义（与对话工具 rerun_video 同口径，收编成 REST 可走的业务体）：
      · 该镜最新任务还在失败/取消态 → 直接重试那条（不新建意图）；
      · 否则 = 对已批准提示词**合法重出片**：走 submit_video_job 全流程，
        幂等由 claim 表 + 在途同意图复用兜住（绝不双份钱）；
      · 提示词没批准 → 409（人工闸门 D-43 不因「重出」破例）。

    组感知：shot_id 可以是组内任何机位——提交单元是组头（一条视频覆盖
    整段戏），这里把成员 id 归到它所在的生成单元再投，报错信息用人话
    说明「重出的是整段片段」。

    返回 {"job_id", "status", "reused", "shot_id", "unit_shot_id", "note"}。
    """
    store = deps.store()
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    shot_id = (shot_id or "").strip()

    # 版本：显式指定 > 当前最新带提示词的版本
    versions = sorted(store.list_video_versions(project_id), key=lambda v: v.created_at)
    version = None
    if version_id:
        version = next((v for v in versions if v.id == version_id), None)
        if version is None:
            raise HTTPException(404, "视频版本不存在")
    else:
        for v in reversed(versions):
            if v.shots and any(isinstance(s, dict) and unit_prompt(s)
                               for s in v.shots if isinstance(s, dict)):
                version = v
                break
    if version is None or not version.shots:
        raise HTTPException(404, "还没有镜头方案：先拆解镜头并生成提示词")

    plan = [s for s in version.shots if isinstance(s, dict)]
    if not any(str(s.get("id")) == shot_id for s in plan):
        raise HTTPException(404, f"镜头 {shot_id} 不在当前方案里")

    # 组感知：把机位 id 归到它所在的生成单元（提交单位 = 单元组头）
    unit = next((u for u in generation_units(plan) if shot_id in u["member_ids"]), None)
    if unit is None or not unit["prompt"]:
        raise HTTPException(409, "该镜头还没有提示词，无法重出片")
    head_id = unit["shot_id"]
    head = next(s for s in plan if str(s.get("id")) == head_id)

    # 最新任务：失败/取消 → 重试原任务（不新建）
    latest = None
    for j in sorted(store.list_video_jobs(project_id), key=lambda x: x.created_at):
        if j.shot_id == head_id:
            latest = j
    if latest is not None and latest.status in ("failed", "cancelled"):
        retry_video_job(latest.id)
        return {"job_id": latest.id, "status": "pending", "reused": True,
                "shot_id": shot_id, "unit_shot_id": head_id,
                "note": "之前那次失败了，已重新排队"}

    if head.get("prompt_status") != "approved":
        raise HTTPException(
            409,
            "该镜头所在片段的提示词还没批准，重出片前需要先批准"
            + ("（这是镜头组的一员，批准的是整段）" if unit["group_id"] else ""),
        )

    provider, model = pick_video_provider()
    job, created = submit_video_job(project_id, VideoJobCreate(
        shot_id=head_id, provider=provider, model=model,
        prompt=unit["prompt"], version_id=version.id,
    ))
    note = (
        f"已提交重出片（{head_id}）"
        + ("——提交单位是整段片段，组内机位一起重出" if unit["group_id"] else "")
    ) if created else "该片段已有在途的同意图任务，复用没有重复提交"
    return {"job_id": job.id, "status": job.status if not created else "submitted",
            "reused": not created, "shot_id": shot_id, "unit_shot_id": head_id,
            "note": note}


# ---------- 批量生成（自动 / 审批模式） ----------


def _unit_intent(
    unit: dict[str, Any], version_shots: list[Any], *, project_id: str,
    resolution: str | None, aspect_ratio: str | None, fallback_duration: float,
    script: Any, registry: dict[str, str], provider: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """批量口径下该单元「这次要投」的意图参数与手挑参考视频。

    必须与 submit_video_job 的落库口径逐键对齐：落库时发生了什么回填，
    比对侧就要算出同样的值——口径不对称，幂等要么漏挡要么把没变的任务
    误判成新意图白烧钱。参考视频只算手挑那段（自动接力锚不进指纹，
    见 _INTENT_PARAM_KEYS 注释）。
    """
    head = next((s for s in version_shots if isinstance(s, dict)
                 and str(s.get("id") or "") == unit["shot_id"]), {})
    if unit["group_id"]:
        members = [s for s in version_shots if isinstance(s, dict)
                   and str(s.get("group_id") or "") == unit["group_id"]
                   and str(s.get("scene_id") or "") == unit.get("scene_id", "")]
    else:
        members = [head]
    # 参考图：显式并集优先；并集为空时 submit 侧会回填定妆图注册表，
    # 比对侧算同一份，否则注册表回填过的任务永远被判「意图不同」。
    raw_images = list(dict.fromkeys(
        u for m in members for u in (m.get("reference_images") or []) if u))
    style = deps.store().get_video_style(project_id) or {}
    if script is not None and (style.get("character_looks") or style.get("scene_look_ids") or style.get("shot_look_ids")):
        from ..domain import Shot
        from ..media.refs import apply_reference_images

        plan = [Shot.model_validate(s) for s in version_shots if isinstance(s, dict)]
        selected_images: list[str] = []
        for raw in members:
            selected = Shot.model_validate(raw)
            apply_reference_images([selected], registry, script, style=style, shot_plan=plan)
            selected_images.extend(selected.reference_images)
        intent_images = list(dict.fromkeys(selected_images)) or None
    elif raw_images:
        intent_images: list[str] | None = raw_images
    else:
        from .continuity import resolve_submission_references

        intent_images = resolve_submission_references(
            shot_id=unit["shot_id"], shot_plan=version_shots,
            completed_urls={}, image_registry=registry, script=script,
        ).get("reference_images") or None
    # 参考音频：镜头显式带过的优先；没有时按提交侧同一口径算音色锚
    # （synthesize=False：比对侧绝不写选角表、绝不烧 TTS）。算不出来
    # （老任务上线前提交、缓存里没有样音）时与 _intent_params_equal 的
    # 「存过为空一律同意图」豁免同口径，不会误判成新意图。
    intent_audios = list(head.get("reference_audios") or [])
    if not intent_audios:
        intent_audios = voice_audios_for(
            deps.store(), project_id, [s for s in version_shots if isinstance(s, dict)],
            unit["shot_id"], script, provider=provider, synthesize=False,
        )
    params = {
        "duration_sec": int(round(unit["duration_sec"] or fallback_duration)),
        "resolution": resolution or default_resolution(),
        "aspect_ratio": aspect_ratio or "",
        "first_frame_image": head.get("first_frame_image") or None,
        "last_frame_image": head.get("last_frame_image") or None,
        "reference_images": intent_images,
        "reference_audios": intent_audios or None,
        "content": None,
    }
    manual = _manual_reference_videos(project_id, head) if head else []
    return params, manual


def pick_video_provider() -> tuple[str, str]:
    """选视频 provider：按 DB 里 **enabled** 的行选（默认第一个）。

    绝不写死厂商名——写死过一次（Agent 侧提交硬编码 "minimax"），结果用户在
    UI 里明明选了方舟、Agent 提交却打去了 MiniMax 那户已欠费的 key 上，
    直接 402（2026-11-19 实测）。配置只走 DB 是 D-48 的规矩。
    """
    rows = [r for r in deps.store().list_api_providers(kind="video") if getattr(r, "enabled", True)]
    return (rows[0].name, "default") if rows else ("minimax", "default")


def _batch_submit(
    project_id: str,
    provider: str,
    model: str,
    version_id: str | None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
) -> Any:
    """构造批次提交回调，只为已批准 Prompt 的镜头创建任务。"""
    store = deps.store()

    # 版本快照在批次启动时取一次：批次期间镜头计划不会变（改 Prompt 会开新版本）。
    if version_id:
        version = store.get_video_version(version_id)
        shots = version.shots if version else []
    else:
        versions = sorted(store.list_video_versions(project_id), key=lambda v: v.created_at)
        shots = versions[-1].shots if versions else []

    # 单元索引：批次推进传的是单元 id（组头），prompt 取折叠后的单元文本
    # （组=group_prompt，单镜=video_prompt）——与 generate_batch 的名单同源。
    unit_by_id = {u["shot_id"]: u for u in generation_units(shots)}

    def _submit(shot_id: str) -> str | None:
        shot = next((s for s in shots if isinstance(s, dict) and s.get("id") == shot_id), None)
        unit = unit_by_id.get(shot_id)
        if shot is None or not unit or not unit["prompt"] or shot.get("prompt_status") != "approved":
            return None
        try:
            job, _created = submit_video_job(project_id, VideoJobCreate(
                shot_id=shot_id, provider=provider, model=model,
                prompt=unit["prompt"], version_id=version_id,
                params={
                    **({"resolution": resolution} if resolution else {}),
                    **({"aspect_ratio": aspect_ratio} if aspect_ratio else {}),
                },
                # 镜头级参考图随镜头计划透传；
                # 参考视频由 _auto_references 统一合并（用户挑的参考素材 + 自动接力）。
                # 组单元：成员参考图并集去重（各机位可能点不同角色）；
                # 成员按 (scene_id, group_id) 认——组键只在场景内唯一。
                reference_images=list(dict.fromkeys(
                    u for m in ([shot] if not unit["group_id"] else
                                [s for s in shots if isinstance(s, dict)
                                 and str(s.get("group_id") or "") == unit["group_id"]
                                 and str(s.get("scene_id") or "") == unit.get("scene_id", "")])
                    for u in (m.get("reference_images") or []) if u
                )),
            ))
            return job.id
        except Exception as e:  # noqa: BLE001
            log.warning("批次提交镜头 %s 失败：%s", shot_id, e)
            return None

    return _submit


def prepare_voice_samples(
    project_id: str, shot_plan: list[dict[str, Any]], shot_ids: list[str],
    script: Any, provider: str,
) -> list[str]:
    """批次提交前的音色预生成：把整批说话人的样音先合成好（只返回已就绪清单）。

    用户 2026-11-25 的口径：能传参考音频就先出好声音、再提交视频生成。
    这里一次把整批说话人的样音合成入库，提交阶段只做内联；TTS 失败只告警
    返回缺项，绝不阻断出片。厂商不支持参考音频时直接返回空。
    """
    if not _provider_supports_reference_audio(provider) or script is None:
        return []
    try:
        store = deps.store()
        from ..media.casting import cast_and_persist

        cast = cast_and_persist(store, project_id, script)
        casting_map = {m.char_id: m.voice_id for m in cast.members if m.char_id}
        ready: list[str] = []
        for sid in shot_ids:
            speakers = _shot_speakers(shot_plan, sid, script)
            budget = _sample_char_budget(len(speakers))
            for cid in speakers:
                voice_id = casting_map.get(cid, "")
                url = _voice_sample(
                    store, project_id, cid, voice_id,
                    text=_character_lines(script, cid, max_chars=budget),
                )
                if url:
                    ready.append(f"{cid}::{voice_id or '未定'}")
        return sorted(set(ready))
    except Exception as e:  # noqa: BLE001
        log.warning("音色预生成失败（不阻断出片）：%s", str(e)[:120])
        return []


def generate_batch(project_id: str, payload: BatchCreateRequest) -> dict[str, Any]:
    """按最新视频版本的镜头计划批量生成。串行推进，前镜成片自动作为后镜接力参考。"""
    store = deps.store()
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    if payload.version_id:
        version = store.get_video_version(payload.version_id)
        if version is None:
            raise HTTPException(404, "视频版本不存在")
        # 批次必须用本作品的方案：拿别的作品的已批准版本在这里开跑会跨作品混数据、
        # 计费归属错乱（闸门 2026-11-11 补，fail-closed）。
        if version.project_id != project_id:
            raise HTTPException(404, "视频版本不属于该作品")
    else:
        versions = sorted(store.list_video_versions(project_id), key=lambda v: v.created_at)
        version = versions[-1] if versions else None
    if version is None or not version.shots:
        raise HTTPException(400, "还没有镜头方案：请先完成导演与摄影指导阶段")

    # 提交名单按**生成单元**折叠（S2）：同组成员只投组头一次——逐镜提交
    # 会把一段戏生成 N 遍（N 倍计费），这是镜头组架构必须堵的洞。
    # 已出片单元的跳过规则（2026-11-14 收口，果园v3 5.43 + 孔乙己 5.44 +
    # 用户看片「重复完成相同场景任务」三轮踩坑的共同正解）：**同意图成片
    # 存在就跳过，跨版本生效**——
    #   · 续跑/重开批次：意图没变，重投=纯浪费（旧版按 version_id 过滤，
    #     续跑开新批准版本后旧成片全放行，正是双烧的洞）；
    #   · 改词/换厂商/换模型/换分辨率重出：意图对不上旧成片，照常提交——
    #     只比 prompt 是老病根，换 Seedance 重跑时旧 MiniMax 成片会把整批
    #     挡掉，用户拿到全是不属于本批的旧镜头；
    #   · 手工提交/对账回写的无 version_id 任务：意图相同同样认。
    # 想对已成功镜头原词重出，走单镜重试入口，不靠重开批次。
    units_all = generation_units(version.shots)
    if payload.episode_id:
        # 分集批量：只保留成员属于该集的生成单元。一个组跨集时按「任一成员
        # 属于本集」整组保留——拆组会让组提示词与机位不完整。
        # 老作品没有 episode_id（全集一集到底）：忽略过滤，避免把整部作品挡掉。
        episode_of = {
            str(s.get("id")): str(s.get("episode_id") or "")
            for s in version.shots if isinstance(s, dict) and s.get("id")
        }
        if any(episode_of.values()):
            units_all = [
                u for u in units_all
                if any(episode_of.get(mid) == payload.episode_id for mid in u["member_ids"])
            ]
            if not units_all:
                raise HTTPException(400, "这一集还没有镜头方案")
    unit_by_id = {u["shot_id"]: u for u in units_all}
    provider = payload.provider or pick_video_provider()[0]
    model = payload.model or "default"
    from .rhythm import suggest_duration

    fallback_dur = suggest_duration(getattr(version, "pacing", None),
                                    getattr(project, "adaptation_type", None))
    registry = dict((store.get_video_style(project_id) or {}).get("reference_images") or {})
    script = None
    try:
        sv = store.latest_version(project)
        script = sv.script if sv is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("批次跳过判据：剧本解析失败（%s），参考图按显式并集比对", str(e)[:80])
    intent_cache: dict[str, tuple[dict[str, Any], list[str]]] = {}

    def _intent_of(shot_id: str) -> tuple[dict[str, Any], list[str]]:
        if shot_id not in intent_cache:
            intent_cache[shot_id] = _unit_intent(
                unit_by_id[shot_id], version.shots, project_id=project_id,
                resolution=payload.resolution, aspect_ratio=payload.aspect_ratio,
                fallback_duration=fallback_dur, script=script, registry=registry,
                provider=provider,
            )
        return intent_cache[shot_id]

    done_here: set[str] = set()
    for j in store.list_video_jobs(project_id):
        unit = unit_by_id.get(j.shot_id)
        if j.status != "succeeded" or not j.video_url or unit is None:
            continue
        intent_params, manual = _intent_of(j.shot_id)
        if _same_generation_intent(
            j, prompt=unit["prompt"], provider=provider, model=model,
            effective_params=intent_params, manual_reference_videos=manual,
        ):
            done_here.add(j.shot_id)
    units = [
        u for u in units_all
        if u["prompt"] and unit_approved(version.shots, u) and u["shot_id"] not in done_here
    ]
    shot_ids = [u["shot_id"] for u in units]
    if not shot_ids:
        raise HTTPException(400, "还没有批准任何视频 Prompt，请先逐镜编辑并批准")

    # 音色先就绪、再提交视频（2026-11-25）：整批说话人的样音在批次启动前
    # 一次合成完，提交时只内联不阻塞；TTS 失败只告警，不阻断出片。
    ready_voices = prepare_voice_samples(
        project_id, version.shots, shot_ids, script, provider,
    )
    if ready_voices:
        log.info("音色样音已就绪：%s", "、".join(ready_voices))

    # 同一项目同时只能有一个批次在推进：两个批次会重复提交同一批镜头（重复计费），
    # 还会互相抢完成回调。要重出片先 abort 掉正在跑的那个。
    running = deps.batch_manager().running_for_project(project_id)
    if running is not None:
        raise HTTPException(
            409,
            f"已有一个出片批次在推进（{running.id}，当前镜头 {running.current_shot_id or '—'}）。"
            "重复提交会重复计费；想换一批请先终止当前批次。",
        )

    batch = deps.batch_manager().start(
        project_id, shot_ids, payload.mode,
        _batch_submit(project_id, provider, model, version.id, payload.resolution, payload.aspect_ratio),
    )
    if batch.status == "failed":
        raise HTTPException(500, batch.error or "批次启动失败")
    return batch.to_dict()
