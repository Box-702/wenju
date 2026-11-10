# =====================================================================
# base.py —— 视频生成 Provider 抽象基类
#
# 所有视频生成适配器继承 VideoProvider，实现统一的异步接口：
#   create_job  → 提交生成任务
#   poll_job    → 查询任务状态
#   cancel_job  → 取消任务
#   download    → 下载生成结果
#
# 参考 Runway / Kling / Sora 等 API 的共性模式：
# 全部走异步提交 + 轮询，返回视频 URL。
# =====================================================================

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# 画幅默认竖屏。短剧是竖屏产品，所有提交入口（Agent / API / 批量）共用这一个默认值，
# 避免「忘了传画幅就出横屏」——以前三处默认值各写一遍 "16:9"，改一处漏两处。
DEFAULT_ASPECT = "9:16"


def default_resolution() -> str:
    """全局默认生成分辨率（VIDEO_DEFAULT_RESOLUTION，默认 768P 低挡位）。

    只是缺省值，不写死：调用方在 params 里显式指定的 resolution 优先；
    换 provider 时按其方言设置该配置项。
    """
    from ..config import get_settings

    return (get_settings().video_default_resolution or "").strip() or "768P"


# ---- 请求 / 响应数据结构 ----


# 服务商对「参考视频」的硬约束（MiniMax H3 V2 实测）：
# 参考视频时长必须落在 2–15 秒，超了整次生成直接被拒。
REFERENCE_VIDEO_MIN_SEC = 2.0
REFERENCE_VIDEO_MAX_SEC = 15.0


@dataclass
class VideoJobParams:
    """视频生成请求参数（统一格式）。

    除基础参数外，包含多模态输入入口（对应 MiniMax H3 V2 接口的
    content 数组：文本 / 图片 / 视频 / 音频）：
      - image_url        首帧图（图生视频）
      - last_frame_image 尾帧图（首尾帧控制）
      - reference_*      多模态参考生视频（参考图 / 参考视频 / 参考音频）
      - content          原生 content 数组直通（优先级最高，格式随 Provider）
    """

    resolution: str = "1280x720"  # WxH 格式
    duration_sec: int = 5
    fps: int = 24
    # 画幅默认竖屏：产品只做短剧，短剧是竖屏的。以前默认 16:9，谁忘了传
    # 画幅参数就会拿到一版横屏成片（2026-11-01 实测：走 API 单镜提交时
    # params 里没有 aspect_ratio，成片出来是 1344x768 的横屏）。
    aspect_ratio: str = DEFAULT_ASPECT
    seed: int | None = None
    negative_prompt: str | None = None
    image_url: str | None = None  # 图生视频的首帧输入图
    last_frame_image: str | None = None  # 尾帧图（首尾帧控制）
    reference_images: list[str] = field(default_factory=list)  # 参考图（≤9）
    reference_videos: list[str] = field(default_factory=list)  # 参考视频（≤3）
    reference_audios: list[str] = field(default_factory=list)  # 参考音频（≤3）
    content: list[dict[str, Any]] | None = None  # 原生多模态 content 数组直通
    extra: dict[str, Any] = field(default_factory=dict)  # provider 特有参数


@dataclass
class VideoJobResponse:
    """提交任务后的响应。"""

    external_task_id: str  # provider 返回的任务 ID
    status: str = "queued"  # queued / pending / generating
    message: str = ""
    cost_estimate: float | None = None  # 预估费用（美元）


@dataclass
class VideoJobStatus:
    """任务状态查询结果。"""

    external_task_id: str
    status: str  # queued / generating / succeeded / failed / cancelled
    progress: float = 0.0  # 0.0 ~ 1.0
    video_url: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # 原始响应


# ---- 抽象基类 ----


class VideoProvider(ABC):
    """视频生成 Provider 抽象基类。

    子类必须实现四个异步方法和一组类属性。
    类属性描述 provider 的能力边界，用于前端展示和路由选择。
    """

    # ---- 子类必须设置的类属性 ----
    name: str = ""  # 唯一标识，如 "runway" / "kling"
    label: str = ""  # 显示名，如 "Runway Gen-4" / "Kling AI"
    supports_text_to_video: bool = True
    supports_image_to_video: bool = False
    max_duration_sec: float = 10.0
    pricing_per_sec: float = 0.0  # 美元/秒，0 表示未知
    supported_resolutions: list[str] = field(default_factory=lambda: ["1280x720", "1920x1080"])
    chinese_prompt_support: bool = False

    def __init__(self, api_key: str = "", base_url: str = "", **kwargs: Any) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") if base_url else ""
        # 用户在设置里选的模型名（api_providers.config_json 的 model 字段），
        # 空 = 由各 provider 用自己的默认模型。
        self.model: str = kwargs.get("model") or ""
        self._config = kwargs

    @abstractmethod
    async def create_job(self, prompt: str, params: VideoJobParams | None = None) -> VideoJobResponse:
        """提交视频生成任务。

        Args:
            prompt: 视频描述文本。
            params: 生成参数（分辨率、时长等）。

        Returns:
            VideoJobResponse 含 external_task_id。
        """
        ...

    @abstractmethod
    async def poll_job(self, external_task_id: str) -> VideoJobStatus:
        """轮询任务状态。

        Args:
            external_task_id: create_job 返回的任务 ID。

        Returns:
            VideoJobStatus 含最新状态和视频 URL（如果完成）。
        """
        ...

    @abstractmethod
    async def cancel_job(self, external_task_id: str) -> bool:
        """取消正在运行的任务。返回是否成功。"""
        ...

    def estimate_cost(self, duration_sec: float) -> float | None:
        """估算生成费用。返回 None 表示无法估算。"""
        if self.pricing_per_sec <= 0:
            return None
        return round(self.pricing_per_sec * duration_sec, 4)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} label={self.label!r}>"
