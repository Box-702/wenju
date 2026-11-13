# =====================================================================
# seedance.py —— 火山方舟 Doubao Seedance 2.0 系列视频生成适配器
#
# API（火山方舟 Ark，国内直连、无需代理；2026-11-13 按官网文档校准）：
#   创建：POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks
#   查询：GET  https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{id}
#   认证：Authorization: Bearer <ARK_API_KEY>
#
# 规格实底（docs.volcengine.com Seedance 2.0 系列页，勿凭记忆改）：
#   模型 id：doubao-seedance-2-0-260128（标准）/ -fast-260128 / -mini-260615
#   duration：4–15 秒（三档同）
#   resolution：2.0 支持 480p/720p/1080p/4K；fast、mini 仅 480p/720p
#   画幅字段名是 **ratio**（21:9/16:9/4:3/1:1/3:4/9:16/adaptive），
#     不是 MiniMax 的 aspect_ratio——两家接口区分点之一
#   content 数组：text（无 role）；image_url role=first_frame/last_frame/
#     reference_image；video_url role=reference_video；audio_url role=
#     reference_audio；图 ≤9、视频 ≤3、音频 ≤3
#   查询响应视频直链在 **content.video_url**（与 MiniMax task.content.url 不同）
#   计费：**按 token**——tokens ≈ (输入视频秒+输出秒) × 宽 × 高 × fps(24) / 1024；
#     720p 档单价：输入不含视频 ¥46/M、含视频 ¥28/M（480p 同价；1080p 51/31）。
#     官方示例核对：5s 720p 16:9 无视频输入 = 1280×720×24×5/1024 ≈ 10.8万 tok
#     × 46/M ≈ ¥4.97 ✓
#
# 与 MiniMax 适配器的 provider 区分：name="seedance"、独立 base_url/模型表；
# 上层按 VideoJob.provider 字段取实例，互不串道。
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

# 2.0 系列三档（官网 2026-11 时点 id）。默认标准档对齐 H3 的质量预期。
DEFAULT_MODEL = "doubao-seedance-2-0-260128"

# 档位 → (每秒 token 单价 元/M：不含视频输入, 含视频输入)。480p/720p 同价。
_TOKEN_PRICE = {
    "480p": (46.0, 28.0),
    "720p": (46.0, 28.0),
    "1080p": (51.0, 31.0),
    "1440p": (26.0, 16.0),   # 4K 档（方舟以像素计，2K 映射到 4K 价保守取高）
}
# fast/mini 单价系数（官网 720p：fast 37/22、mini 23/14 ≈ 0.80 / 0.50）
_MODEL_FACTOR = {"fast": 0.80, "mini": 0.50, "2-0": 1.0}

# 分辨率档 → 像素面积（宽×高，横竖屏面积同）
_PIXELS = {"480p": 854 * 480, "720p": 1280 * 720, "1080p": 1920 * 1080, "1440p": 3840 * 2160}
FPS = 24  # token 公式口径（官方示例反推）

# 统一分辨率（WxH / 各家档位名）→ 方舟档位。768P 是 MiniMax 方言，对应方舟 720p。
_RESOLUTION_MAP = {
    "480p": "480p", "768p": "720p", "720p": "720p",
    "1080p": "1080p", "2k": "1440p", "4k": "1440p",
    "854x480": "480p", "1280x720": "720p", "1920x1080": "1080p",
}


def _map_resolution(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v in _RESOLUTION_MAP:
        return _RESOLUTION_MAP[v]
    return v if v.endswith("p") and v[:-1].isdigit() else "720p"


class SeedanceProvider(VideoProvider):
    """火山方舟 Seedance 2.0 系列视频生成（异步任务 + 轮询）。"""

    name = "seedance"
    label = "火山方舟（豆包）"
    supports_text_to_video = True
    supports_image_to_video = True
    max_duration_sec = 15.0
    pricing_per_sec = 0.99  # 720p 标准档粗算口径，仅展示；实算走 estimate_cost_real
    supported_resolutions = ["480p", "720p", "1080p"]
    chinese_prompt_support = True

    def __init__(self, api_key: str = "", base_url: str = "", **kwargs: Any) -> None:
        # key 只从调用方注入（DB api_providers 行，D-48：env 不再是配置入口）。
        # 老安装迁移由 llm/seed.py 负责搬家，这里不再读 ARK/SEEDANCE 变量。
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

    def _model_factor(self) -> float:
        m = (self.model or "").lower()
        for key, f in _MODEL_FACTOR.items():
            if key in m:
                return f
        return 1.0

    def estimate_cost_real(self, probe: VideoJobParams, duration_sec: float,
                           resolution: str = "") -> float:
        """按官网 token 公式实算：tokens = (输出秒 + 参考视频输入秒) × 宽×高×24/1024。

        参考视频存在时整单按「含视频输入」的较低单价（官方价目口径）。
        """
        res = _map_resolution(resolution or "")
        base_no, base_with = _TOKEN_PRICE.get(res, _TOKEN_PRICE["720p"])
        factor = self._model_factor()
        area = _PIXELS.get(res, _PIXELS["720p"])
        in_video_sec = 0.0
        for u in (probe.reference_videos or []):
            # 接力锚已裁尾 2.5s（_job_params），按裁后口径估
            in_video_sec += 2.5 if str(u).startswith("data:") else 5.0
        tokens = (max(0.0, duration_sec) + in_video_sec) * area * FPS / 1024
        rate = base_with if in_video_sec > 0 else base_no
        return round(tokens / 1e6 * rate * factor, 4)

    async def create_job(self, prompt: str, params: VideoJobParams | None = None) -> VideoJobResponse:
        params = params or VideoJobParams()
        # 官网区间 4–15s（低于 4 会被拒；组单元时长由导演闸门保证）
        duration = max(4, min(15, int(params.duration_sec)))
        resolution = _map_resolution(params.resolution)
        body: dict[str, Any] = {
            "model": self.model,
            "content": self._content(prompt, params),
            "resolution": resolution,
            "duration": duration,
            "ratio": params.aspect_ratio or "16:9",
        }
        if params.seed is not None:
            body["seed"] = params.seed
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
                cost_estimate=self.estimate_cost_real(params, duration, resolution),
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
