# =====================================================================
# deps.py —— 依赖单例（全项目唯一的装配点）
#
# 用 lru_cache 保证整个进程只创建一份后端实例。
# 上层（API / CLI / Agent）只从这里取依赖，不自己 new 服务。
# =====================================================================

from __future__ import annotations

import logging
from functools import lru_cache

from .agent.tasks import SubAgentRunner, get_runner
from .config import Settings, get_settings
from .llm import LLM, build_llm
from .store import Store
from .video import ProviderRegistry
from .video import get_registry as get_video_registry
from .video.queue import VideoJobManager
from .video.batch import get_batch_manager
from .video.queue import get_manager as get_video_manager

log = logging.getLogger(__name__)


@lru_cache
def settings() -> Settings:
    return get_settings()


@lru_cache
def store() -> Store:
    return Store(settings().database_url)


@lru_cache
def llm() -> LLM:
    # 模型配置权威是数据库 api_providers（D-41 翻转）；.env 只在空库时兜底。
    # 用户在「设置 → 模型」改动后走 reset_llm() 重建本单例。
    return build_llm(settings(), store())


@lru_cache
def subagent_runner() -> SubAgentRunner:
    runner = get_runner()
    # 落库（任务起止快照）是增强而非必需：数据库不可用时退化为纯内存，
    # 不能让 /api/tasks 这类只读接口跟着 500。
    try:
        runner.attach_storage(store())
    except Exception as e:  # noqa: BLE001
        log.warning("后台任务落库不可用（%s），任务历史将仅保留在内存", e)
    return runner


@lru_cache
def video_registry() -> ProviderRegistry:
    return get_video_registry()


@lru_cache
def video_manager() -> VideoJobManager:
    return get_video_manager()


@lru_cache
def batch_manager():
    return get_batch_manager()


@lru_cache
def finalize_manager():
    from .video.finalize import get_finalize_manager

    return get_finalize_manager()
