# =====================================================================
# video.py —— 视频制作（Provider / 模型偏好 / 视频版本 / 生成任务）
#
# 覆盖三块：
#   - Provider 配置（LLM / 视频 / 图片的 key 与端点，存 DB）
#   - 模型偏好（为某类任务指定默认 provider + model）
#   - 视频版本快照与生成任务（提交 → 轮询 → 状态回调写回 DB）
# =====================================================================

from __future__ import annotations

import difflib
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..crew.dramaturgy import refresh_prompt_audits
from ..video.base import DEFAULT_ASPECT, default_resolution

from . import common, deps
from .schemas import (
    ModelPreferenceSet,
    ProviderCreate,
    ProviderUpdate,
    VideoPromptBulkReview,
    VideoPromptReview,
    VideoJobCreate,
    VideoVersionCreate,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _prompt_summary(shots: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总一个视频版本里的 Prompt 审阅状态。"""
    prompts = [
        shot for shot in shots
        if isinstance(shot, dict) and shot.get("video_prompt")
    ]
    statuses = [shot.get("prompt_status") or "needs_review" for shot in prompts]
    approved = sum(status == "approved" for status in statuses)
    needs_revision = sum(status == "needs_revision" for status in statuses)
    pending = len(prompts) - approved
    if not prompts:
        review_status = "empty"
    elif approved == len(prompts):
        review_status = "approved"
    elif approved == 0 and needs_revision == len(prompts):
        review_status = "needs_revision"
    elif approved == 0 and needs_revision == 0:
        review_status = "needs_review"
    else:
        review_status = "mixed"
    return {
        "prompt_count": len(prompts),
        "approved_count": approved,
        "pending_count": pending,
        "needs_revision_count": needs_revision,
        "review_status": review_status,
    }


def _shot_from_version(version: Any, shot_id: str) -> dict[str, Any] | None:
    return next(
        (
            shot for shot in version.shots
            if isinstance(shot, dict) and shot.get("id") == shot_id
        ),
        None,
    )


# ---------- Provider 管理 ----------


@router.post("/providers")
def create_provider(payload: ProviderCreate) -> dict[str, Any]:
    """添加一个新的 API Provider。"""
    p = deps.store().create_api_provider(
        kind=payload.kind,
        name=payload.name,
        label=payload.label,
        base_url=payload.base_url,
        api_key=payload.api_key,
        config=payload.config,
        enabled=payload.enabled,
    )
    deps.reset_llm()
    return {"id": p.id, "name": p.name, "label": p.label, "kind": p.kind}


@router.get("/providers")
def list_providers(kind: str | None = None) -> list[dict[str, Any]]:
    """列出所有 API Providers（不返回 key，只给 configured 标记）。"""
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "name": r.name,
            "label": r.label,
            "base_url": r.base_url,
            "configured": bool(r.api_key),
            "enabled": r.enabled,
            "config": r.config,
            "created_at": r.created_at.isoformat(),
        }
        for r in deps.store().list_api_providers(kind=kind)
    ]


@router.get("/providers/{provider_id}")
def get_provider(provider_id: str) -> dict[str, Any]:
    """获取 Provider 详情。"""
    p = deps.store().get_api_provider(provider_id)
    if not p:
        raise HTTPException(404, "Provider 不存在")
    return {
        "id": p.id,
        "kind": p.kind,
        "name": p.name,
        "label": p.label,
        "base_url": p.base_url,
        "configured": bool(p.api_key),
        "enabled": p.enabled,
        "config": p.config,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


@router.patch("/providers/{provider_id}")
def update_provider(provider_id: str, payload: ProviderUpdate) -> dict[str, Any]:
    """更新 Provider 配置。"""
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    p = deps.store().update_api_provider(provider_id, **fields)
    if not p:
        raise HTTPException(404, "Provider 不存在")
    deps.reset_llm()
    return {"id": p.id, "ok": True}


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: str) -> dict[str, Any]:
    """删除 Provider。"""
    if not deps.store().delete_api_provider(provider_id):
        raise HTTPException(404, "Provider 不存在")
    deps.reset_llm()
    return {"ok": True}


def _probe_openai_compatible(base_url: str, api_key: str, model: str = "") -> tuple[bool, str, list[str]]:
    """真的打一次厂商接口（OpenAI 兼容协议）。

    返回 (成功?, 说明, 模型名列表)。失败时说明是人话，直接给用户看。
    """
    import json as _json
    from urllib.error import HTTPError, URLError
    from urllib.request import Request as _Request, urlopen as _urlopen

    base = (base_url or "").rstrip("/")
    if not base:
        return False, "没有填 Base URL", []
    if not api_key:
        return False, "没有配置 API Key", []

    def _get(path: str) -> tuple[int, str]:
        req = _Request(base + path, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with _urlopen(req, timeout=15) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except URLError as e:
            return 0, str(getattr(e, "reason", e))
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    status, body = _get("/models")
    if status == 200:
        try:
            data = _json.loads(body)
            names = [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
            return True, f"连接成功，厂商返回 {len(names)} 个模型", names[:12]
        except Exception:  # noqa: BLE001
            return True, "连接成功", []
    if status in (401, 403):
        return False, "Key 被拒绝（401/403）：key 不对、已过期，或没开通这个模型", []
    if status in (404, 405):
        # 不支持 /models：退一步，用最小的 chat 请求探一次
        payload = _json.dumps({
            "model": model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }).encode("utf-8")
        req = _Request(
            base + "/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with _urlopen(req, timeout=20) as r:
                if r.status == 200:
                    return True, "连接成功（该厂商不支持 /models，用最小请求验证）", []
                return False, f"连接失败：HTTP {r.status}", []
        except HTTPError as e:
            code = e.code
            if code in (401, 403):
                return False, "Key 被拒绝（401/403）：key 不对、已过期，或没开通这个模型", []
            detail = e.read().decode("utf-8", "replace")[:160]
            return False, f"连接失败：HTTP {code} {detail}", []
        except URLError as e:
            return False, f"连不上：{getattr(e, 'reason', e)}", []
        except Exception as e:  # noqa: BLE001
            return False, f"连接失败：{e}", []
    if status == 0:
        return False, f"连不上：{body[:120]}", []
    return False, f"连接失败：HTTP {status} {body[:120]}", []


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str) -> dict[str, Any]:
    """测试 Provider 连接：**真的打一次厂商接口**，不是只看 key 有没有填。"""
    p = deps.store().get_api_provider(provider_id)
    if not p:
        raise HTTPException(404, "Provider 不存在")
    if p.kind == "video":
        from ..video import get_registry

        provider = get_registry().get_provider(p.name, api_key=p.api_key, base_url=p.base_url)
        if not provider:
            return {"ok": False, "error": f"未找到视频 Provider: {p.name}"}
        if not p.api_key:
            return {"ok": False, "error": "未配置 API Key"}
        # MiniMax 没有通用探活接口，只能到这里为止——如实说，别假装测过了。
        return {
            "ok": True,
            "provider": p.name,
            "label": provider.label,
            "detail": "视频厂商没有统一的探活接口，这里只校验了 key 已配置与厂商可解析；"
                      "真实可用性要等出片时才知道",
        }

    model = str((p.config or {}).get("model") or "")
    ok, detail, models = _probe_openai_compatible(p.base_url, p.api_key, model)
    out: dict[str, Any] = {
        "ok": ok,
        "provider": p.name,
        "label": p.label,
        "detail": detail,
    }
    if models:
        out["models"] = models
    if not ok:
        out["error"] = detail
    return out


# ---------- 模型偏好 ----------


@router.get("/models/preferences")
def list_preferences() -> list[dict[str, Any]]:
    """列出所有模型偏好设置。"""
    return [
        {
            "id": p.id,
            "task_type": p.task_type,
            "provider_id": p.provider_id,
            "model_name": p.model_name,
            "params": p.params,
            "is_default": p.is_default,
        }
        for p in deps.store().list_model_preferences()
    ]


@router.put("/models/preferences")
def set_preference(payload: ModelPreferenceSet) -> dict[str, Any]:
    """设置模型偏好。"""
    try:
        pref = deps.store().set_model_preference(
            task_type=payload.task_type,
            provider_id=payload.provider_id,
            model_name=payload.model_name,
            params=payload.params,
            is_default=payload.is_default,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    deps.reset_llm()
    return {"id": pref.id, "task_type": pref.task_type, "ok": True}


@router.delete("/models/preferences/{task_type}")
def clear_preference(task_type: str) -> dict[str, Any]:
    """清除某类任务的模型偏好，回落到 .env 的厂商优先级链。

    存在的理由：模型偏好（界面上选的）优先级高于 `.env`，一旦选过就一直生效。
    用户改了 `.env` 里的模型名却不生效时，没有这个出口只能删库。
    """
    removed = deps.store().clear_model_preference(task_type)
    deps.reset_llm()
    return {"task_type": task_type, "removed": removed, "ok": True}


@router.get("/models/available")
def available_models() -> dict[str, Any]:
    """列出所有可用的模型 Provider（按 kind 分组）。"""
    llm_providers: list[dict[str, Any]] = []
    video_providers: list[dict[str, Any]] = []
    for p in deps.store().list_api_providers():
        info = {
            "id": p.id,
            "name": p.name,
            "label": p.label,
            "configured": bool(p.api_key),
            "enabled": p.enabled,
        }
        if p.kind == "llm":
            llm_providers.append(info)
        elif p.kind == "video":
            video_providers.append(info)
    return {"llm": llm_providers, "video": video_providers}


# ---------- 视频版本 ----------


@router.post("/projects/{project_id}/video-versions")
def create_video_version(project_id: str, payload: VideoVersionCreate) -> dict[str, Any]:
    """创建视频版本快照。"""
    common.get_project(project_id)
    v = deps.store().create_video_version(
        project_id=project_id,
        shots=payload.shots,
        style_guide=payload.style_guide,
        source_type=payload.source_type,
        label=payload.label,
        notes=payload.notes,
        parent_version_id=payload.parent_version_id,
    )
    return {"id": v.id, "project_id": project_id, "created_at": v.created_at.isoformat()}


@router.get("/projects/{project_id}/video-versions")
def list_video_versions(project_id: str) -> list[dict[str, Any]]:
    """列出项目的所有视频版本。"""
    project = common.get_project(project_id)
    return [
        {
            "id": v.id,
            "parent_version_id": v.parent_version_id,
            "source_type": v.source_type,
            "label": v.label,
            "notes": v.notes,
            "milestone": v.milestone,
            "shot_count": len(v.shots),
            "current": v.id == project.current_video_version_id,
            "prompt_summary": _prompt_summary(v.shots),
            "created_at": v.created_at.isoformat(),
        }
        for v in deps.store().list_video_versions(project_id)
    ]


@router.get("/video-versions/{version_id}")
def get_video_version(version_id: str) -> dict[str, Any]:
    """获取视频版本详情（含完整镜头数据）。"""
    v = deps.store().get_video_version(version_id)
    if not v:
        raise HTTPException(404, "视频版本不存在")
    return {
        "id": v.id,
        "project_id": v.project_id,
        "parent_version_id": v.parent_version_id,
        "source_type": v.source_type,
        "label": v.label,
        "notes": v.notes,
        "milestone": v.milestone,
        "shots": v.shots,
        "style_guide": v.style_guide,
        "created_at": v.created_at.isoformat(),
    }


def _normalize_reference_videos(urls: list[str] | None) -> list[str]:
    """清洗用户自备参考视频 URL：去空白、去重、只留 http(s)、上限 3 条。"""
    cleaned: list[str] = []
    for raw in urls or []:
        url = str(raw or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        if url not in cleaned:
            cleaned.append(url)
        if len(cleaned) >= 3:
            break
    return cleaned


def _style_context(version: Any) -> str:
    """取版本风格指南里的全片风格段（camera_style）。

    审计「一镜多运镜」时要排除它——风格段描述的是整部片的运镜语法，
    每个镜头都会带一遍，不排除会把所有镜头都误判成一镜多运镜。
    """
    guide = getattr(version, "style_guide", None)
    if guide is None and isinstance(version, dict):
        guide = version.get("style_guide")
    camera_style = getattr(guide, "camera_style", None)
    if camera_style is None and isinstance(guide, dict):
        camera_style = guide.get("camera_style")
    return str(camera_style or "")


@router.put("/projects/{project_id}/video-versions/{version_id}/shots/{shot_id}/prompt")
def review_video_prompt(
    project_id: str,
    version_id: str,
    shot_id: str,
    payload: VideoPromptReview,
) -> dict[str, Any]:
    """保存并审阅一个镜头的 Prompt，生成新的可回滚视频版本。"""
    common.get_project(project_id)
    current = deps.store().get_video_version(version_id)
    if current is None or current.project_id != project_id:
        raise HTTPException(404, "视频版本不存在")

    shots = [dict(shot) for shot in current.shots if isinstance(shot, dict)]
    target = next((shot for shot in shots if shot.get("id") == shot_id), None)
    if target is None:
        raise HTTPException(404, "镜头不存在")

    target["video_prompt"] = payload.prompt.strip()
    target["prompt_status"] = (
        "approved" if payload.decision == "approve" else
        "needs_revision" if payload.decision == "needs_revision" else
        "needs_review"
    )
    target["prompt_review_note"] = payload.note.strip()
    if payload.reference_videos is not None:
        target["reference_videos"] = _normalize_reference_videos(payload.reference_videos)
    # 人工改完 prompt 后重跑确定性审计，结论随新版本落库（出厂前双重审计的人工侧）。
    # 传入风格指南的 camera_style，排除「本镜没有的运镜」——风格段每个镜头都带一遍。
    refresh_prompt_audits(shots, style_context=_style_context(current))

    labels = {
        "approve": "批准镜头提示词",
        "needs_revision": "要求修改镜头提示词",
        "save": "保存镜头提示词草稿",
    }
    version = deps.store().create_video_version(
        project_id=project_id,
        shots=shots,
        style_guide=current.style_guide,
        source_type="manual",
        label=labels[payload.decision],
        notes=payload.note.strip() or None,
        parent_version_id=current.id,
    )
    approved = sum(1 for shot in shots if shot.get("prompt_status") == "approved")
    needs_review = sum(
        1 for shot in shots
        if shot.get("video_prompt") and shot.get("prompt_status") != "approved"
    )
    return {
        "version_id": version.id,
        "shot_id": shot_id,
        "prompt_status": target["prompt_status"],
        "approved_count": approved,
        "needs_review_count": needs_review,
    }


@router.post("/projects/{project_id}/video-versions/{version_id}/prompts/review")
def bulk_review_video_prompts(
    project_id: str,
    version_id: str,
    payload: VideoPromptBulkReview,
) -> dict[str, Any]:
    """批量审阅多个镜头的 Prompt，并生成新的可回滚视频版本。"""
    common.get_project(project_id)
    current = deps.store().get_video_version(version_id)
    if current is None or current.project_id != project_id:
        raise HTTPException(404, "视频版本不存在")

    requested_ids = list(dict.fromkeys(payload.shot_ids))
    shots = [dict(shot) for shot in current.shots if isinstance(shot, dict)]
    targets = [
        shot for shot in shots
        if shot.get("id") in requested_ids and shot.get("video_prompt")
    ]
    if not targets:
        raise HTTPException(400, "选中的镜头没有可审阅的 Prompt")

    status = "approved" if payload.decision == "approve" else "needs_revision"
    for shot in targets:
        shot["prompt_status"] = status
        shot["prompt_review_note"] = payload.note.strip()
    # 批量决策不改 prompt 内容，但也要把全部镜头的审计结论刷新到同一基线。
    refresh_prompt_audits(shots, style_context=_style_context(current))

    labels = {
        "approve": "批量批准镜头提示词",
        "needs_revision": "批量要求修改镜头提示词",
    }
    version = deps.store().create_video_version(
        project_id=project_id,
        shots=shots,
        style_guide=current.style_guide,
        source_type="manual",
        label=labels[payload.decision],
        notes=payload.note.strip() or None,
        parent_version_id=current.id,
    )
    summary = _prompt_summary(shots)
    return {
        "version_id": version.id,
        "reviewed_shot_ids": [shot["id"] for shot in targets],
        "reviewed_count": len(targets),
        **summary,
    }


@router.get(
    "/projects/{project_id}/video-versions/{version_id}/shots/{shot_id}/prompt-history"
)
def video_prompt_history(
    project_id: str,
    version_id: str,
    shot_id: str,
) -> dict[str, Any]:
    """沿视频版本父链返回一个镜头的 Prompt 审阅历史与逐次差异。"""
    common.get_project(project_id)
    current = deps.store().get_video_version(version_id)
    if current is None or current.project_id != project_id:
        raise HTTPException(404, "视频版本不存在")

    history: list[dict[str, Any]] = []
    version = current
    seen: set[str] = set()
    while version and version.id not in seen:
        seen.add(version.id)
        shot = _shot_from_version(version, shot_id)
        if shot is not None and shot.get("video_prompt"):
            parent = (
                deps.store().get_video_version(version.parent_version_id)
                if version.parent_version_id
                else None
            )
            previous_shot = _shot_from_version(parent, shot_id) if parent else None
            previous_prompt = (previous_shot or {}).get("video_prompt", "")
            diff_lines = list(
                difflib.unified_diff(
                    previous_prompt.splitlines(),
                    str(shot.get("video_prompt") or "").splitlines(),
                    fromfile="上一版本",
                    tofile="当前版本",
                    lineterm="",
                )
            )
            history.append(
                {
                    "version_id": version.id,
                    "parent_version_id": version.parent_version_id,
                    "label": version.label,
                    "source_type": version.source_type,
                    "created_at": version.created_at.isoformat(),
                    "prompt": shot.get("video_prompt", ""),
                    "prompt_status": shot.get("prompt_status") or "needs_review",
                    "prompt_review_note": shot.get("prompt_review_note") or "",
                    "previous_prompt": previous_prompt,
                    "prompt_changed": previous_prompt != shot.get("video_prompt", ""),
                    "diff": diff_lines,
                }
            )
        version = (
            deps.store().get_video_version(version.parent_version_id)
            if version.parent_version_id
            else None
        )

    if not history:
        raise HTTPException(404, "镜头不存在或没有 Prompt 审阅历史")
    return {"shot_id": shot_id, "version_id": version_id, "history": history}


@router.post("/video-versions/{version_id}/milestone")
def set_video_milestone(version_id: str, milestone: str | None = None) -> dict[str, Any]:
    """设置视频版本里程碑。"""
    v = deps.store().set_video_version_milestone(version_id, milestone)
    if not v:
        raise HTTPException(404, "视频版本不存在")
    return {"id": v.id, "milestone": v.milestone}


# ---------- 视频生成任务 ----------


def _job_params(payload: dict[str, Any]) -> Any:
    """把落库的 params 映射成 Provider 统一入参（多余键进 extra）。"""
    from ..video.base import VideoJobParams

    # 这些键有专门的归处：既不要重复塞进 extra，也不要当作未知字段透传给 provider。
    handled_keys = {
        "duration_sec", "resolution", "ratio", "aspect_ratio", "fps", "seed", "negative_prompt",
        "first_frame_image", "last_frame_image",
        "reference_images", "reference_videos", "reference_audios", "content",
    }
    return VideoJobParams(
        duration_sec=int(payload.get("duration_sec") or 6),
        resolution=str(payload.get("resolution") or "1280x720"),
        aspect_ratio=str(payload.get("ratio") or payload.get("aspect_ratio") or DEFAULT_ASPECT),
        fps=int(payload.get("fps") or 24),
        seed=payload.get("seed"),
        negative_prompt=payload.get("negative_prompt"),
        image_url=payload.get("first_frame_image"),
        last_frame_image=payload.get("last_frame_image"),
        reference_images=payload.get("reference_images") or [],
        reference_videos=payload.get("reference_videos") or [],
        reference_audios=payload.get("reference_audios") or [],
        content=payload.get("content"),
        extra={k: v for k, v in payload.items() if k not in handled_keys},
    )


def _resolve_video_provider(name: str) -> Any:
    """按 DB 配置解析视频 Provider 实例（缺省回落到环境变量）。"""
    from ..video import get_registry

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


def _create_and_submit_job(project_id: str, payload: VideoJobCreate) -> Any:
    """创建视频任务并投递（单镜提交与批量批次共用的入口）。

    含提交时自动锚回填（定妆图 + 前镜成片接力）与默认分辨率策略：
    payload.params 未显式带 resolution 时落全局默认（VIDEO_DEFAULT_RESOLUTION）。

    时长优先级：调用方显式指定 > 导演给该镜头的 duration_sec > 节奏默认值。
    这里曾经写死 `params.get("duration_sec") or 6`，导演设计的 8–10 秒镜头
    一律被压成 6 秒送进模型——动作被硬塞进更短的时间，只能糊成一团。
    """
    from ..video import get_registry
    from ..video.rhythm import suggest_duration

    version = _resolve_video_version(project_id, payload.version_id)
    shot = next(
        (s for s in (version.shots if version else [])
         if isinstance(s, dict) and s.get("id") == payload.shot_id),
        None,
    )
    if shot and shot.get("video_prompt") and shot.get("prompt_status") != "approved":
        raise HTTPException(409, "该镜头的 Prompt 尚未批准，请先完成人工审阅")

    # 时长：显式 > 导演 > 节奏默认（按本场 pacing + 项目改编类型给的建议值）。
    # 短剧项目默认 4–5 秒一镜——短视频时代的注意力只给开头三秒，不能用电影的
    # 8 秒基准（2026-10-31 用户反馈）。
    explicit = payload.params.get("duration_sec")
    director_sec = float((shot or {}).get("duration_sec") or 0)
    pacing = getattr(version, "pacing", None) if version else None
    project = deps.store().get_project(project_id)
    fmt = getattr(project, "adaptation_type", None)
    duration = int(round(explicit or director_sec or suggest_duration(pacing, fmt)))

    provider = get_registry().get_provider(payload.provider)
    cost = provider.estimate_cost(float(duration)) if provider else None

    stored_params = dict(payload.params)
    # 落库固定下来：重试、批量重投都要用同一个时长，不能再各自回落到默认值。
    stored_params["duration_sec"] = duration
    stored_params.setdefault("resolution", default_resolution())
    for key in ("first_frame_image", "last_frame_image", "reference_images",
                "reference_videos", "reference_audios", "content"):
        value = getattr(payload, key)
        if value:
            stored_params[key] = value

    # 提交时自动补齐一致性锚（定妆图 + 前镜成片接力）；调用方显式给的优先。
    _auto_references(project_id, payload.shot_id, payload.version_id, stored_params)

    job = deps.store().create_video_job(
        project_id=project_id,
        shot_id=payload.shot_id,
        provider=payload.provider,
        model=payload.model,
        prompt=payload.prompt,
        params=stored_params,
        version_id=payload.version_id,
        cost_estimate=cost,
    )
    _submit_video_job(job)
    return job


def _resolve_video_version(project_id: str, version_id: str | None) -> Any:
    """取指定或最新视频版本，供出片前的 Prompt 门槛复用。"""
    store = deps.store()
    if version_id:
        version = store.get_video_version(version_id)
        if version and version.project_id == project_id:
            return version
        return None
    versions = sorted(store.list_video_versions(project_id), key=lambda v: v.created_at)
    return versions[-1] if versions else None


def _job_dict(j: Any) -> dict[str, Any]:
    return {
        "id": j.id,
        "shot_id": j.shot_id,
        "provider": j.provider,
        "model": j.model,
        "prompt": j.prompt,
        "status": j.status,
        "video_url": j.video_url,
        "error_message": j.error_message,
        "cost_estimate": j.cost_estimate,
        "params": j.params or {},
        "created_at": j.created_at.isoformat(),
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


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
    }
    registry = dict((store.get_video_style(project_id) or {}).get("reference_images") or {})

    script = None
    try:
        script = store.latest_version(common.get_project(project_id)).script
    except Exception:  # noqa: BLE001
        log.warning("自动参考回填：取剧本版本失败，跳过定妆图回填")

    from ..video.continuity import resolve_submission_references

    refs = resolve_submission_references(
        shot_id=shot_id,
        shot_plan=[s for s in version.shots if isinstance(s, dict)],
        completed_urls=completed_urls,
        image_registry=registry,
        script=script,
    )
    for key in ("reference_images", "reference_videos"):
        if refs.get(key) and not stored_params.get(key):
            stored_params[key] = refs[key]


@router.post("/projects/{project_id}/video/generate")
def create_video_job(project_id: str, payload: VideoJobCreate) -> dict[str, Any]:
    """提交视频生成任务：落库后立刻投递到异步队列（提交 → 轮询 → 回调写回状态）。"""
    common.get_project(project_id)
    job = _create_and_submit_job(project_id, payload)
    return {
        "id": job.id,
        "status": "submitted",
        "provider": job.provider,
        "model": job.model,
        "cost_estimate": job.cost_estimate,
    }


@router.get("/projects/{project_id}/video/jobs")
def list_video_jobs(project_id: str) -> list[dict[str, Any]]:
    """列出项目的所有视频生成任务。"""
    common.get_project(project_id)
    return [_job_dict(j) for j in deps.store().list_video_jobs(project_id)]
@router.get("/video/jobs/{job_id}")
def get_video_job(job_id: str) -> dict[str, Any]:
    """查询视频生成任务状态。"""
    j = deps.store().get_video_job(job_id)
    if not j:
        raise HTTPException(404, "任务不存在")
    return {"project_id": j.project_id, **_job_dict(j)}


@router.post("/video/jobs/{job_id}/cancel")
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
    deps.store().update_video_job(job_id, status="cancelled", finished_at=common.now())
    return {"id": job_id, "status": "cancelled"}


@router.post("/video/jobs/{job_id}/retry")
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

    def _submit(shot_id: str) -> str | None:
        shot = next((s for s in shots if isinstance(s, dict) and s.get("id") == shot_id), None)
        if shot is None or not shot.get("video_prompt") or shot.get("prompt_status") != "approved":
            return None
        try:
            job = _create_and_submit_job(project_id, VideoJobCreate(
                shot_id=shot_id, provider=provider, model=model,
                prompt=shot.get("video_prompt") or "", version_id=version_id,
                params={
                    **({"resolution": resolution} if resolution else {}),
                    **({"aspect_ratio": aspect_ratio} if aspect_ratio else {}),
                },
                # 镜头级用户自备参考素材随镜头计划透传；
                # 与自动接力（_auto_references）不冲突：显式给的键优先。
                reference_videos=[u for u in shot.get("reference_videos") or [] if u],
                reference_images=[u for u in shot.get("reference_images") or [] if u],
            ))
            return job.id
        except Exception as e:  # noqa: BLE001
            log.warning("批次提交镜头 %s 失败：%s", shot_id, e)
            return None

    return _submit


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


@router.post("/projects/{project_id}/video/generate-batch")
def generate_batch(project_id: str, payload: BatchCreateRequest) -> dict[str, Any]:
    """按最新视频版本的镜头计划批量生成。串行推进，前镜成片自动作为后镜接力参考。"""
    common.get_project(project_id)
    store = deps.store()
    if payload.version_id:
        version = store.get_video_version(payload.version_id)
        if version is None:
            raise HTTPException(404, "视频版本不存在")
    else:
        versions = sorted(store.list_video_versions(project_id), key=lambda v: v.created_at)
        version = versions[-1] if versions else None
    if version is None or not version.shots:
        raise HTTPException(400, "还没有镜头方案：请先完成导演与摄影指导阶段")

    shot_ids = [
        s["id"] for s in version.shots
        if isinstance(s, dict)
        and s.get("id")
        and s.get("video_prompt")
        and s.get("prompt_status") == "approved"
    ]
    if not shot_ids:
        raise HTTPException(400, "还没有批准任何视频 Prompt，请先逐镜编辑并批准")

    provider = payload.provider or _pick_video_provider()[0]
    model = payload.model or "default"
    batch = deps.batch_manager().start(
        project_id, shot_ids, payload.mode,
        _batch_submit(project_id, provider, model, version.id, payload.resolution, payload.aspect_ratio),
    )
    if batch.status == "failed":
        raise HTTPException(500, batch.error or "批次启动失败")
    return batch.to_dict()


@router.get("/projects/{project_id}/video/batch")
def get_latest_batch(project_id: str) -> dict[str, Any]:
    """查询项目最新批次的进度（前端轮询用）。"""
    common.get_project(project_id)
    batch = deps.batch_manager().latest_for_project(project_id)
    if batch is None:
        return {"id": None, "status": "none"}
    return batch.to_dict()


class BatchCommandRequest(BaseModel):
    command: str = Field(pattern=r"^(approve|reroll|abort)$")


@router.post("/video/batches/{batch_id}/command")
def batch_command(batch_id: str, payload: BatchCommandRequest) -> dict[str, Any]:
    """向批次下达命令：approve 放行下一镜 / reroll 重跑当前镜 / abort 终止。"""
    mgr = deps.batch_manager()
    batch = mgr.get(batch_id)
    if batch is None:
        raise HTTPException(404, "批次不存在")

    # 在状态变更前捕获当前任务与最新版本：approve/reroll 沿用原 provider/model。
    prev = deps.store().get_video_job(batch.job_id) if batch.job_id else None
    provider = prev.provider if prev else "minimax"
    model = prev.model if prev else "default"
    prev_resolution = ((prev.params or {}).get("resolution") if prev else None) or None
    prev_aspect = ((prev.params or {}).get("aspect_ratio") if prev else None) or None
    versions = sorted(deps.store().list_video_versions(batch.project_id), key=lambda v: v.created_at)
    version_id = versions[-1].id if versions else None

    def _resubmit(shot_id: str) -> str | None:
        return _batch_submit(batch.project_id, provider, model, version_id, prev_resolution, prev_aspect)(shot_id)

    try:
        if payload.command == "approve":
            batch = mgr.approve(batch_id, _resubmit)
        elif payload.command == "reroll":
            batch = mgr.reroll(batch_id, _resubmit)
        else:
            batch = mgr.abort(batch_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return batch.to_dict()


# ---- 成片合成（拼装 + 可选配音/字幕） ----


class FinalizeCreate(BaseModel):
    """成片合成请求。version_id 缺省用项目当前视频版本。

    subtitles：台词字幕烧录（默认开）；dubbing：台词配音（默认开，无台词/无 TTS 自动跳过）。
    """

    version_id: str | None = None
    subtitles: bool = True
    dubbing: bool = True


@router.post("/projects/{project_id}/video/finalize")
def finalize_film(project_id: str, payload: FinalizeCreate) -> dict[str, Any]:
    """把已生成的镜头合成为完整成片。后台执行，返回任务 id 供轮询。"""
    common.get_project(project_id)
    if payload.version_id:
        version = deps.store().get_video_version(payload.version_id)
        if version is None:
            raise HTTPException(404, "视频版本不存在")
    rec = deps.finalize_manager().start(
        project_id, version_id=payload.version_id,
        subtitles=payload.subtitles, dubbing=payload.dubbing,
    )
    return rec


@router.get("/video/finalizes/{finalize_id}")
def get_finalize(finalize_id: str) -> dict[str, Any]:
    """查询合成任务进度与结果（前端轮询用）。"""
    rec = deps.finalize_manager().get(finalize_id)
    if rec is None:
        raise HTTPException(404, "合成任务不存在（重启后历史任务作废，产物已在素材里）")
    return rec


@router.get("/projects/{project_id}/video/finalize/latest")
def get_latest_finalize(project_id: str) -> dict[str, Any]:
    """查询项目最近一次合成任务（刷新页面后恢复状态用）。"""
    common.get_project(project_id)
    rec = deps.finalize_manager().latest_for_project(project_id)
    if rec is None:
        return {"id": None, "status": "none"}
    return rec
