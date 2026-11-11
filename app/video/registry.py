# =====================================================================
# registry.py —— 视频生成 Provider 注册表
#
# 管理所有已注册的 VideoProvider 实例。
# 支持按 name 查找、动态注册、以及从数据库 api_providers 表加载配置。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

from .base import VideoProvider

log = logging.getLogger(__name__)


class ProviderRegistry:
    """视频生成 Provider 注册表。

    两种注册方式：
      1. 类注册：register_class(RunwayProvider) → 按需实例化
      2. 实例注册：register_instance(provider) → 直接持有
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[VideoProvider]] = {}
        self._instances: dict[str, VideoProvider] = {}

    # ---- 类注册（延迟实例化） ----

    def register_class(self, cls: type[VideoProvider]) -> None:
        """注册一个 Provider 类。实例化延迟到 get_provider() 调用时。"""
        name = cls.name
        if not name:
            raise ValueError(f"{cls.__name__} 必须设置 name 类属性")
        self._classes[name] = cls
        log.debug("注册视频 Provider 类：%s (%s)", name, cls.__name__)

    # ---- 查找 ----

    def get_provider(self, name: str, **kwargs: Any) -> VideoProvider | None:
        """按名称获取 Provider 实例。

        优先返回已注册的实例；没有则尝试用注册的类 + kwargs 实例化。
        已缓存实例可能是在无 key 情况下创建的（例如仅用于费用估算），
        此时若调用方带了 key 配置，就地补齐，避免拿不到鉴权。
        """
        if name in self._instances:
            inst = self._instances[name]
            api_key = kwargs.get("api_key") or ""
            if api_key and not inst.api_key:
                inst.api_key = api_key
                base_url = kwargs.get("base_url") or ""
                if base_url:
                    inst.base_url = base_url.rstrip("/")
            model = kwargs.get("model") or ""
            if model:
                inst.model = model
            return inst
        cls = self._classes.get(name)
        if cls is None:
            return None
        try:
            instance = cls(**kwargs)
            self._instances[name] = instance
            return instance
        except Exception as e:  # noqa: BLE001
            log.warning("实例化 Provider %s 失败：%s", name, e)
            return None

    def reset(self) -> None:
        """清空已实例化的 Provider（保留类注册）。

        用户改了 api_providers 表里的 key/base_url 后必须调它：
        `get_provider` 对已缓存实例只在 api_key 为空时补 key（见上方注释），
        不调 reset 的话，在跑的 MiniMax 实例会继续用旧 key 出片直到进程重启
        （chat 侧有 reset_llm 收口，video 侧此前没有对等机制）。
        """
        self._instances.clear()

    def list_available(self) -> list[dict[str, Any]]:
        """列出所有已注册的 Provider 信息（不含 API Key）。"""
        result: list[dict[str, Any]] = []
        seen: set[str] = set()

        # 已实例化的
        for name, inst in self._instances.items():
            result.append(self._describe(inst))
            seen.add(name)

        # 仅有类注册的
        for name, cls in self._classes.items():
            if name not in seen:
                result.append({
                    "name": cls.name,
                    "label": cls.label,
                    "configured": False,
                    "supports_text_to_video": cls.supports_text_to_video,
                    "supports_image_to_video": cls.supports_image_to_video,
                    "max_duration_sec": cls.max_duration_sec,
                    "pricing_per_sec": cls.pricing_per_sec,
                    "supported_resolutions": cls.supported_resolutions,
                    "chinese_prompt_support": cls.chinese_prompt_support,
                })
        return result

    @staticmethod
    def _describe(provider: VideoProvider) -> dict[str, Any]:
        return {
            "name": provider.name,
            "label": provider.label,
            "configured": bool(provider.api_key),
            "supports_text_to_video": provider.supports_text_to_video,
            "supports_image_to_video": provider.supports_image_to_video,
            "max_duration_sec": provider.max_duration_sec,
            "pricing_per_sec": provider.pricing_per_sec,
            "supported_resolutions": provider.supported_resolutions,
            "chinese_prompt_support": provider.chinese_prompt_support,
        }


# ---- 全局单例 ----

_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    """获取全局 ProviderRegistry 单例。首次调用时自动注册所有内置 Provider。"""
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
        _register_builtins(_registry)
    return _registry


def _register_builtins(registry: ProviderRegistry) -> None:
    """注册内置 Provider 类。产品只走 MiniMax（D-08），不再内置其他厂商。"""
    from .providers.minimax import MiniMaxProvider

    registry.register_class(MiniMaxProvider)
