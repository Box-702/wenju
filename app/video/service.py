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

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .. import deps
from .base import DEFAULT_ASPECT, default_resolution

log = logging.getLogger(__name__)


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


# ---------- Provider 解析与任务投递 ----------


def _job_params(payload: dict[str, Any]) -> Any:
    """把落库的 params 映射成 Provider 统一入参（多余键进 extra）。

    参考素材是本机文件（回放地址 /api/assets/file/xxx），服务商拉不到这种相对
    地址（实测 MiniMax 判 invalid param），所以在这里统一内联成 data URI。

    接力参考视频的计费裁剪（2026-11-12 账单复盘）：MiniMax 按
    input_seconds + output_seconds 同价计费，整条 4-6s 成片当接力锚比输出
    还贵（实测 ¥8.3/镜里 ¥4.5 是输入视频）。衔接只需要前镜**结尾**，
    公网直链在这里裁尾 2.5s 内联；用户手挑的参考素材不动（那是创作输入，
    不是接力锚）。
    """
    from ..media.inline import clip_tail_data_uri, inline_media_urls
    from .base import VideoJobParams

    # 这些键有专门的归处：既不要重复塞进 extra，也不要当作未知字段透传给 provider。
    handled_keys = {
        "duration_sec", "resolution", "ratio", "aspect_ratio", "fps", "seed", "negative_prompt",
        "first_frame_image", "last_frame_image",
        "reference_images", "reference_videos", "reference_audios", "content",
    }
    ref_videos: list[str] = []
    for u in inline_media_urls(deps.store(), payload.get("reference_videos") or []):
        if u.startswith("http"):
            # 接力成片直链：裁尾内联；裁失败回退整条（可用性 > 省钱）
            ref_videos.append(clip_tail_data_uri(u) or u)
        else:
            ref_videos.append(u)
    return VideoJobParams(
        duration_sec=int(payload.get("duration_sec") or 6),
        resolution=str(payload.get("resolution") or "1280x720"),
        aspect_ratio=str(payload.get("ratio") or payload.get("aspect_ratio") or DEFAULT_ASPECT),
        fps=int(payload.get("fps") or 24),
        seed=payload.get("seed"),
        negative_prompt=payload.get("negative_prompt"),
        image_url=payload.get("first_frame_image"),
        last_frame_image=payload.get("last_frame_image"),
        reference_images=inline_media_urls(deps.store(), payload.get("reference_images") or []),
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

    deps.video_manager().submit(job.id, provider, job.prompt, _job_params(dict(job.params or {})))


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


def _same_intent(job: Any, payload: VideoJobCreate, effective_params: dict[str, Any],
                 manual_reference_videos: list[str]) -> bool:
    """在途任务与新请求是不是「同一件事」（claim 之前的前置比对）。

    claim 表只覆盖**从 submit_video_job 进来**的轮；成片质检的自动重 roll
    （app/video/qa.py）直接 create_video_job、不占 claim。没有这层比对的话：
    质检刚重 roll 出一个在途任务，用户又说「出片」——claim 查不到、任务又不是
    自己提交的，就会第三次建档、同一个镜头两条任务并行烧钱。
    """
    if job.prompt != payload.prompt:
        return False
    if job.provider != payload.provider or job.model != payload.model:
        return False
    stored = dict(job.params or {})

    def _norm(value: Any) -> Any:
        return value if value else None

    for key in _INTENT_PARAM_KEYS:
        if _norm(stored.get(key)) != _norm(effective_params.get(key)):
            return False
    # 参考视频：存下来的是「手挑 + 自动接力锚」合并结果，接力锚随出片进度变化不比，
    # 只按长度截出手挑那段比对。
    if manual_reference_videos:
        stored_refs = list(stored.get("reference_videos") or [])
        if stored_refs[: len(manual_reference_videos)] != manual_reference_videos:
            return False
    return True


# ---------- 提交（单镜 / 镜头组，S2 PLAN-shot-group） ----------


def _generation_units(shots: list[Any]) -> list[dict[str, Any]]:
    """把镜头列表折叠成生成单元：单镜自成单元；同 group_id 的机位合成一个单元。

    单元 = 一次视频生成。组单元的 prompt 取组头 group_prompt（DP 折叠产物，
    缺则回落组头 video_prompt），时长 = 组内各机位之和；job 的 shot_id 用
    组头 id，成员 id 列表进 params（finalize/配音按成员展开，S2b）。
    旧数据（无 group 字段）逐字节等价于「每镜一单元」——兼容是设计出来的。
    """
    by_gid: dict[str, list[dict[str, Any]]] = {}
    units: list[dict[str, Any]] = []
    for s in shots:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        gid = str(s.get("group_id") or "")
        if gid:
            by_gid.setdefault(gid, []).append(s)
        else:
            units.append(_unit_of([s]))
    for members in by_gid.values():
        members.sort(key=lambda x: (int(x.get("group_index") or 0),
                                    int(x.get("order") or 0)))
        units.append(_unit_of(members))
    units.sort(key=lambda u: u["head_order"])
    return units


def _unit_of(members: list[dict[str, Any]]) -> dict[str, Any]:
    head = members[0]
    prompt = str(head.get("group_prompt") or "") if len(members) > 1 else ""
    prompt = prompt or str(head.get("video_prompt") or "")
    return {
        "shot_id": str(head.get("id")),
        "group_id": str(head.get("group_id") or "") if len(members) > 1 else "",
        "member_ids": [str(m.get("id")) for m in members],
        "prompt": prompt,
        "duration_sec": sum(max(0.0, float(m.get("duration_sec") or 0)) for m in members),
        "head_order": int(head.get("order") or 0),
    }


def _unit_approved(all_shots: list[Any], unit: dict[str, Any]) -> bool:
    """单元的批准状态看组头（单镜单元即自己）。组内任何机位没批都不投——
    批了半段戏就提交等于替用户做主。"""
    head = next((s for s in all_shots
                 if isinstance(s, dict) and s.get("id") == unit["shot_id"]), None)
    return bool(head) and head.get("prompt_status") == "approved"


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
    from . import get_registry
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
    members = [s for s in (version.shots or [])
               if isinstance(s, dict) and str(s.get("group_id") or "") == gid] \
        if gid else [shot]
    approved_prompt = (str(shot.get("group_prompt") or "") if gid and len(members) > 1
                       else str(shot.get("video_prompt") or ""))
    if not approved_prompt:
        raise HTTPException(409, "该镜头还没有 Prompt，无法提交出片")
    if shot.get("prompt_status") != "approved":
        raise HTTPException(409, "该镜头的 Prompt 尚未批准，请先完成人工审阅")
    if (payload.prompt or "").strip() != approved_prompt.strip():
        raise HTTPException(409, "提交内容与该镜头已批准的 Prompt 不一致——改过内容需要重新批准")

    # 时长：显式 > 导演（组=各机位之和）> 节奏默认（按本场 pacing + 项目改编类型给的建议值）。
    # 短剧项目默认 4–5 秒一镜——短视频时代的注意力只给开头三秒，不能用电影的
    # 8 秒基准（2026-10-31 用户反馈）。
    explicit = payload.params.get("duration_sec")
    director_sec = sum(float(m.get("duration_sec") or 0) for m in members)
    pacing = getattr(version, "pacing", None) if version else None
    project = store.get_project(project_id)
    fmt = getattr(project, "adaptation_type", None)
    duration = int(round(explicit or director_sec or suggest_duration(pacing, fmt)))

    provider = get_registry().get_provider(payload.provider)

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
    _auto_references(project_id, payload.shot_id, payload.version_id, stored_params)

    # 费用估算放在锚回填**之后**：MiniMax 按输入秒数同价计费（2026-11-12 账单
    # 复盘），参考图/接力视频是回填进来的，回填前估算必然低估。
    if provider is not None:
        try:
            from .base import VideoJobParams as _VJP

            _probe = _VJP(
                duration_sec=duration,
                reference_images=stored_params.get("reference_images") or [],
                reference_videos=stored_params.get("reference_videos") or [],
            )
            cost = (provider.estimate_cost_real(
                        _probe, duration,
                        str(stored_params.get("resolution") or ""))
                    if hasattr(provider, "estimate_cost_real")
                    else provider.estimate_cost(float(duration)))
        except Exception:  # noqa: BLE001
            cost = provider.estimate_cost(float(duration))
    else:
        cost = None

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
            cost_estimate=cost,
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


def _auto_references(project_id: str, shot_id: str | None, version_id: str | None, stored_params: dict[str, Any]) -> None:
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

    completed_urls = {
        j.shot_id: j.video_url
        for j in store.list_video_jobs(project_id)
        if j.status == "succeeded" and j.video_url and j.shot_id
        # 只拦「两个版本都有明确归属且不同代」的成片（果园 v4 排查发现的重号
        # 污染洞）；单镜手提交的任务常不带 version_id，无法证明是别代，放行。
        and not (version_id and j.version_id and j.version_id != version_id)
    }
    registry = dict((store.get_video_style(project_id) or {}).get("reference_images") or {})

    script = None
    try:
        project = store.get_project(project_id)
        script = store.latest_version(project).script if project else None
    except Exception:  # noqa: BLE001
        log.warning("自动参考回填：取剧本版本失败，跳过定妆图回填")

    from .continuity import resolve_submission_references

    refs = resolve_submission_references(
        shot_id=shot_id,
        shot_plan=[s for s in version.shots if isinstance(s, dict)],
        completed_urls=completed_urls,
        image_registry=registry,
        script=script,
    )
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
    for key in ("reference_images",):
        if refs.get(key) and not stored_params.get(key):
            stored_params[key] = refs[key]


# ---------- 取消 / 重试 ----------


def cancel_video_job(job_id: str) -> dict[str, Any]:
    """取消视频生成任务：先请服务商侧停止，再收尾本地状态。"""
    j = deps.store().get_video_job(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    # 服务商侧取消是尽力而为：任务已不在内存队列（如进程重启过）或 provider
    # 不支持取消时，不影响本地状态收尾——本地状态才是前端看到的那份。
    try:
        provider = _resolve_video_provider(j.provider)
        if provider is not None:
            deps.video_manager().cancel(job_id, provider)
    except Exception as e:  # noqa: BLE001
        log.warning("通知 provider 取消任务 %s 失败：%s", job_id, e)
    deps.store().update_video_job(job_id, status="cancelled", finished_at=_now())
    return {"id": job_id, "status": "cancelled"}


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


# ---------- 批量生成（自动 / 审批模式） ----------


def _pick_video_provider() -> tuple[str, str]:
    """选视频 provider：优先 MiniMax（本产品视频链路唯一切入点），否则第一个已配置的。"""
    rows = [r for r in deps.store().list_api_providers(kind="video") if getattr(r, "enabled", True)]
    for row in rows:
        if row.name == "minimax":
            return row.name, "default"
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
    unit_by_id = {u["shot_id"]: u for u in _generation_units(shots)}

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
                # 组单元：成员参考图并集去重（各机位可能点不同角色）。
                reference_images=list(dict.fromkeys(
                    u for m in ([shot] if not unit["group_id"] else
                                [s for s in shots if isinstance(s, dict)
                                 and str(s.get("group_id") or "") == unit["group_id"]])
                    for u in (m.get("reference_images") or []) if u
                )),
            ))
            return job.id
        except Exception as e:  # noqa: BLE001
            log.warning("批次提交镜头 %s 失败：%s", shot_id, e)
            return None

    return _submit


def generate_batch(project_id: str, payload: BatchCreateRequest) -> dict[str, Any]:
    """按最新视频版本的镜头计划批量生成。串行推进，前镜成片自动作为后镜接力参考。"""
    store = deps.store()
    if store.get_project(project_id) is None:
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
    # 已在**本版**出片的单元跳过（与 N2D busy_shots 同源纪律，2026-11-13 果园v3：
    # 批次中断后用户再说「出片」，旧代码把已成功的单元重新投一遍=重复计费；
    # 按 version_id 过滤，所以改提示词开新版本后重跑不受影响）。
    done_here = {
        j.shot_id for j in store.list_video_jobs(project_id)
        if j.status == "succeeded" and j.video_url and j.version_id == version.id
    }
    units = [
        u for u in _generation_units(version.shots)
        if u["prompt"] and _unit_approved(version.shots, u) and u["shot_id"] not in done_here
    ]
    shot_ids = [u["shot_id"] for u in units]
    if not shot_ids:
        raise HTTPException(400, "还没有批准任何视频 Prompt，请先逐镜编辑并批准")

    provider = payload.provider or _pick_video_provider()[0]
    model = payload.model or "default"

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
