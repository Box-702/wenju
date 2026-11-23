# =====================================================================
# deps.py —— API 层的依赖入口
#
# 路由统一通过 `from . import deps` 再 `deps.store()` 取值，而不是
# `from ..deps import store` 直接绑名字。这样有两个好处：
#   1. 测试里 monkeypatch `app.api.deps.store` 就能整体替换依赖，
#      不必逐个路由模块打补丁；
#   2. 依赖查找始终是运行时的，晚绑定不会拿到过期引用。
#
# 真正的单例定义在 app/deps.py（全项目唯一装配点）。
# =====================================================================

from __future__ import annotations

from ..deps import (  # noqa: F401
    batch_manager,
    chat_turn_queue,
    finalize_manager,
    llm,
    settings,
    store,
    subagent_runner,
    video_manager,
)

__all__ = [
    "batch_manager",
    "chat_turn_queue",
    "finalize_manager",
    "llm",
    "settings",
    "store",
    "subagent_runner",
    "video_manager",
]


def reset_llm() -> None:
    """用户增删 provider / 改模型偏好后调用：下一个请求重建 LLM 单例。

    用 getattr 取 cache_clear 而不是直接调：测试里 deps.llm 会被替换成普通
    函数（lambda），直接调 cache_clear 会 AttributeError——而这个函数在
    「设置模型」这类接口的收尾处，炸在这里会让整个请求 500。
    """
    clear = getattr(llm, "cache_clear", None)
    if callable(clear):
        clear()
    # 队列存活时间长于单次请求；切模型后后续排队轮次也应使用新模型。
    from ..deps import chat_turn_queue as queue_factory
    if queue_factory.cache_info().currsize:
        queue_factory().llm = llm()
