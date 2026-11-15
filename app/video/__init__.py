# =====================================================================
# video —— 视频生成 Provider 抽象层
#
# 统一封装各视频生成 API（Runway / Kling / CogVideo / Sora / MiniMax），
# 提供一致的提交 → 轮询 → 下载接口。
# =====================================================================

from .registry import ProviderRegistry, get_registry

__all__ = [
    "ProviderRegistry",
    "get_registry",
]
