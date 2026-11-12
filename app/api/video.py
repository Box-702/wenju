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


def _reset_video_provider_cache() -> None:
    """厂商配置（key/base_url/model 偏好）变更后清掉视频 Provider 实例缓存。

    chat 侧有 deps.reset_llm() 热生效（D-24），video 侧此前没有对等机制：
    get_provider 复用已缓存实例、只在 key 为空时补 key，改过 key 不重启
    就继续拿旧 key 出片。实例是按需重建的、无长连接，清空无副作用。
    """
    try:
        from ..video import get_registry

        get_registry().reset()
    except Exception:  # noqa: BLE001  缓存清理失败不该让配置保存本身失败
        log.warning("视频 Provider 实例缓存清理失败（改 key 后可能要重启才生效）", exc_info=True)


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
    _reset_video_provider_cache()
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


@router.get("/providers/specs")
def provider_specs() -> list[dict[str, Any]]:
    """内置厂商目录（D-42）：设置页的「选厂商 → 填 key → 发现」一步流的数据源。

    必须在 `/providers/{provider_id}` 之前注册——不然 "specs" 会被当成 id 匹配掉。
    只含 OpenAI 兼容协议的对话厂商（cogview 是生图专用、minimax 的对话走
    同一把视频 key 意义不大，都不进目录）。
    """
    from ..llm.providers import get_provider_spec

    catalog = ["deepseek", "zhipu", "qwen", "moonshot", "xiaomi", "doubao",
               "anthropic", "openai"]
    out = []
    for name in catalog:
        spec = get_provider_spec(name)
        if spec is None:
            continue
        out.append({
            "name": spec.name,
            "label": spec.label,
            "base_url": spec.default_base_url,
            "docs": spec.docs,
            "env_key": spec.env_key,
            "chat_models": list(spec.chat_models),
        })
    return out


class ProbeRequest(BaseModel):
    """不建行、直接拿表单里的 base_url + key 问厂商。"""

    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)


@router.post("/providers/probe")
async def probe_provider(payload: ProbeRequest) -> dict[str, Any]:
    """「填 key 就能看有什么模型」：拿未保存的 base_url/key 真打一次 /models。

    这是 D-42 一步流的核心——原来必须先建厂商行才能「发现模型」，
    用户要在两个界面之间来回；现在目录里点厂商、贴 key，模型清单当场出来。
    失败时 error 是人话（_probe 的 401/403 等语义直接透传）。
    """
    ok, detail, models = _probe_openai_compatible(payload.base_url, payload.api_key)
    out: dict[str, Any] = {"ok": ok, "detail": detail}
    if models:
        out["models"] = models
    if not ok:
        out["error"] = detail
    return out


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
    _reset_video_provider_cache()
    return {"id": p.id, "ok": True}


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: str) -> dict[str, Any]:
    """删除 Provider。"""
    if not deps.store().delete_api_provider(provider_id):
        raise HTTPException(404, "Provider 不存在")
    deps.reset_llm()
    _reset_video_provider_cache()
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
            # 不截断：BYOK 的本意是「厂商有什么 → 用户勾什么」，只给前 12 个
            # 等于替用户做了他没要求的筛选（厂商清单本来就是按新→旧排的）。
            return True, f"连接成功，厂商返回 {len(names)} 个模型", names
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


def _normalize_reference_assets(asset_ids: list[str] | None, project_id: str) -> list[str]:
    """校验用户挑的参考素材：必须是本作品的视频素材，且时长/帧率在服务商能收的范围内。

    返回通过校验的素材 id（去重、上限 3 条）。不合格的直接抛 400 —— 与其让用户
    提交后白跑一次生成，不如在保存提示词时就告诉他这段片子为什么不能用。
    """
    from ..media.probe import duration_seconds, video_fps
    from ..video.base import (
        REFERENCE_VIDEO_MAX_FPS,
        REFERENCE_VIDEO_MAX_SEC,
        REFERENCE_VIDEO_MIN_FPS,
        REFERENCE_VIDEO_MIN_SEC,
    )

    store = deps.store()
    cleaned: list[str] = []
    for raw in asset_ids or []:
        asset_id = str(raw or "").strip()
        if not asset_id or asset_id in cleaned:
            continue
        asset = store.get_asset(asset_id)
        if asset is None or asset.project_id != project_id:
            raise HTTPException(404, f"参考素材不存在：{asset_id}")
        if asset.kind != "video":
            raise HTTPException(400, f"参考素材《{asset.name or asset_id}》不是视频")
        data = getattr(asset, "content", None)
        if data:
            seconds = duration_seconds(bytes(data), asset.name or "")
            if seconds is not None and not (REFERENCE_VIDEO_MIN_SEC <= seconds <= REFERENCE_VIDEO_MAX_SEC):
                raise HTTPException(
                    400,
                    f"参考素材《{asset.name or asset_id}》时长 {seconds:.1f} 秒，"
                    f"视频模型只接受 {REFERENCE_VIDEO_MIN_SEC:.0f}–{REFERENCE_VIDEO_MAX_SEC:.0f} 秒的片段",
                )
            fps = video_fps(bytes(data), asset.name or "")
            if fps is not None and not (REFERENCE_VIDEO_MIN_FPS <= fps <= REFERENCE_VIDEO_MAX_FPS):
                raise HTTPException(
                    400,
                    f"参考素材《{asset.name or asset_id}》帧率 {fps:.1f} fps，"
                    f"视频模型只接受 {REFERENCE_VIDEO_MIN_FPS:.1f}–{REFERENCE_VIDEO_MAX_FPS:.0f} fps 的片段",
                )
        cleaned.append(asset_id)
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
    if payload.reference_asset_ids is not None:
        target["reference_asset_ids"] = _normalize_reference_assets(payload.reference_asset_ids, project_id)
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
#
# 提交 / 幂等 / 意图指纹 / 参考回填 / 批次推进等业务逻辑已下沉到
# app/video/service.py（2026-11-11：修复 video/sweeper、chat/tools 反向
# import api 层的分层违规）。这里按旧名字再导出——路由、既有调用方与
# 测试的引用路径保持不变。

from ..video.service import (  # noqa: E402,F401
    BatchCreateRequest,
    VideoJobCreate,
    _batch_submit,
    _job_params,
    _pick_video_provider,
    _resolve_video_provider,
    _same_intent,
    _submit_video_job,
    _video_intent,
    generate_batch,
    submit_video_job,
)
from ..video import service as _video_service  # noqa: E402


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


def _manual_reference_videos(project_id: str, shot: dict[str, Any]) -> list[str]:
    """已下沉到 app/video/service.py；此处仅保留转发（测试引用路径不破）。"""
    return _video_service._manual_reference_videos(project_id, shot)


def _auto_references(project_id: str, shot_id: str | None, version_id: str | None, stored_params: dict[str, Any]) -> None:
    """已下沉到 app/video/service.py；此处仅保留转发（测试引用路径不破）。"""
    return _video_service._auto_references(project_id, shot_id, version_id, stored_params)


@router.post("/projects/{project_id}/video/generate")
def create_video_job(project_id: str, payload: VideoJobCreate) -> dict[str, Any]:
    """提交视频生成任务：落库后立刻投递到异步队列（提交 → 轮询 → 回调写回状态）。

    幂等：同一镜头已有在途任务时返回那个任务（`reused=True`），不新建、不重复计费。
    """
    common.get_project(project_id)
    job, created = submit_video_job(project_id, payload)
    return {
        "id": job.id,
        "status": "submitted" if created else job.status,
        "provider": job.provider,
        "model": job.model,
        "cost_estimate": job.cost_estimate,
        "reused": not created,
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
    """取消视频生成任务（业务体在 app/video/service.py）。"""
    return _video_service.cancel_video_job(job_id)


@router.post("/video/jobs/{job_id}/retry")
def retry_video_job(job_id: str) -> dict[str, Any]:
    """重试失败的视频生成任务（业务体在 app/video/service.py）。"""
    return _video_service.retry_video_job(job_id)


# ---------- 批量生成（自动 / 审批模式） ----------
# 业务体（_pick_video_provider / _batch_submit / BatchCreateRequest / generate_batch）
# 已下沉到 app/video/service.py，顶部统一再导出。


@router.post("/projects/{project_id}/video/generate-batch")
def generate_batch(project_id: str, payload: BatchCreateRequest) -> dict[str, Any]:
    """按最新视频版本的镜头计划批量生成（业务体在 app/video/service.py）。"""
    return _video_service.generate_batch(project_id, payload)


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
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return batch.to_dict()


# ---- 成片合成（拼装 + 可选配音/字幕） ----


class FinalizeCreate(BaseModel):
    """成片合成请求。version_id 缺省用项目当前视频版本。

    subtitles：台词字幕烧录（默认开）；audio_mode：声音模式——
    native（默认，视频模型原生音轨，台词随提示词音画同生）/
    dub（TTS 配音混入）/ silent（静片，字幕承担台词）。
    """

    version_id: str | None = None
    subtitles: bool = True
    audio_mode: str = "native"


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
        subtitles=payload.subtitles, audio_mode=payload.audio_mode,
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
