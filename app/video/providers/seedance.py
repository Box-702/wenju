# =====================================================================
# seedance.py —— 火山方舟 Doubao Seedance 视频生成适配器
#
# API（火山方舟 Ark，国内直连、无需代理）：
#   创建：POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks
#   查询：GET  https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{id}
#   认证：Authorization: Bearer <ARK_API_KEY>
# 请求体与 MiniMax H3 V2 同族的多模态 content 数组（text + image_url/video_url，
# role: first_frame / last_frame / reference_image / reference_video），
# 差异在方言字段：resolution 是 "480p/720p/1080p"、duration 2–12 秒、
# ratio 叫 aspect_ratio、帧率叫 fps（固定 24）。
#
# 状态映射：queued/running/succeeded/cancelled → 统一枚举；**查询响应成功时
# 视频直链在 content.video_url**（与 MiniMax 的 task.content.url 不同源，别抄错）。
#
# ⚠️ 本适配器写于无 key 阶段（2026-11-12），接口形态按方舟公开文档与
#    同族模型惯例实现，解析全部防御式；接入 A/B 前必须先跑
#    scripts/probe_seedance.py 用真 key 校准字段（模型 id / 状态枚举 / URL 路径）。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..base import (
    VideoJobParams,
    VideoJobResponse,
    VideoJobStatus,
    VideoProvider,
)

log = logging.getLogger(__name__)

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
TASKS_PATH = "/contents/generations/tasks"

# 方舟 seedance 系列模型 id（2026-11 时点；用户在设置里可换）。
# pro 质量档 / lite 便宜档，A/B 先用 pro 对齐 H3 的质量预期。
DEFAULT_MODEL = "doubao-seedance-1-0-pro-250528"

# 统一分辨率（WxH / 档位名）→ 方舟档位
_RESOLUTION_MAP = {
    "480p": "480p", "768p": "720p", "720p": "720p",
    "1080p": "1080p", "2k": "1440p",
    "854x480": "480p", "1280x720": "720p", "1920x1080": "1080p",
}


def _map_resolution(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v in _RESOLUTION_MAP:
        return _RESOLUTION_MAP[v]
    # 已是方舟档位（480p/720p/1080p/…）直接透传，否则保守落 720p
    return v if v.endswith("p") and v[:-1].isdigit() else "720p"


class SeedanceProvider(VideoProvider):
    """火山方舟 Seedance 视频生成（异步任务 + 轮询）。"""

    name = "seedance"
    label = "Seedance（火山方舟）"
    supports_text_to_video = True
    supports_image_to_video = True
    max_duration_sec = 12.0
    pricing_per_sec = 0.025  # pro 档粗略估算，仅用于费用预估展示
    supported_resolutions = ["480p", "720p", "1080p"]
    chinese_prompt_support = True

    def __init__(self, api_key: str = "", base_url: str = "", **kwargs: Any) -> None:
        import os

        api_key = api_key or os.environ.get("ARK_API_KEY", "")
        super().__init__(api_key, base_url, **kwargs)
        if not self.base_url:
            self.base_url = ARK_BASE
        if not self.model:
            self.model = DEFAULT_MODEL

    # ---- content 数组：与 MiniMax 适配器同一套输入，映射成方舟方言 ----

    def _content(self, prompt: str, params: VideoJobParams) -> list[dict[str, Any]]:
        if params.content:  # 原生直通（探针脚本用）
            return params.content
        items: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if params.image_url:
            items.append({"type": "image_url", "role": "first_frame",
                          "image_url": {"url": params.image_url}})
        if params.last_frame_image:
            items.append({"type": "image_url", "role": "last_frame",
                          "image_url": {"url": params.last_frame_image}})
        for u in params.reference_images[:9]:
            items.append({"type": "image_url", "role": "reference_image",
                          "image_url": {"url": u}})
        for u in params.reference_videos[:3]:
            items.append({"type": "video_url", "role": "reference_video",
                          "video_url": {"url": u}})
        return items

    async def create_job(self, prompt: str, params: VideoJobParams | None = None) -> VideoJobResponse:
        params = params or VideoJobParams()
        duration = max(2, min(12, int(params.duration_sec)))
        body: dict[str, Any] = {
            "model": self.model,
            "content": self._content(prompt, params),
            "resolution": _map_resolution(params.resolution),
            "duration": duration,
            "fps": 24,
            "aspect_ratio": params.aspect_ratio or "9:16",
        }
        if params.seed is not None:
            body["seed"] = params.seed
        if params.negative_prompt:
            body["negative_prompt"] = params.negative_prompt
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}{TASKS_PATH}",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=body,
            )
            data = resp.json() if "json" in resp.headers.get("content-type", "") else {"raw": resp.text}
            if resp.status_code >= 400:
                msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("message", str(data)[:200])
                raise RuntimeError(f"Seedance 创建任务失败（HTTP {resp.status_code}）：{msg}")
            task_id = str(data.get("id") or "")
            if not task_id:
                raise RuntimeError(f"Seedance 响应没有任务 id：{str(data)[:200]}")
            return VideoJobResponse(
                external_task_id=task_id,
                status=str(data.get("status") or "queued"),
                cost_estimate=self.estimate_cost(duration),
            )

    async def poll_job(self, external_task_id: str) -> VideoJobStatus:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base_url}{TASKS_PATH}/{external_task_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
        status = str(data.get("status") or "").lower()
        # 方舟状态枚举 → 统一枚举（防御式：未知状态按 generating 继续轮询）
        mapping = {"queued": "queued", "running": "generating", "processing": "generating",
                   "succeeded": "succeeded", "success": "succeeded",
                   "failed": "failed", "cancelled": "cancelled", "canceled": "cancelled"}
        mapped = mapping.get(status, "generating" if status else "queued")
        video_url: str | None = None
        err: str | None = None
        content = data.get("content")
        if isinstance(content, dict):
            video_url = content.get("video_url")
        if mapped == "failed":
            e = data.get("error")
            err = (e.get("message") if isinstance(e, dict) else str(e)) if e else str(data)[:200]
        return VideoJobStatus(
            external_task_id=external_task_id,
            status=mapped,
            video_url=video_url,
            error_message=err,
            raw=data if isinstance(data, dict) else {},
        )

    async def cancel_job(self, external_task_id: str) -> bool:
        # 方舟 DELETE 端点存在与否未实测；失败只当没取消，不影响本地收尾。
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    f"{self.base_url}{TASKS_PATH}/{external_task_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return resp.status_code < 400
        except Exception:  # noqa: BLE001
            return False
