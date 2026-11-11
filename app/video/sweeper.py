# =====================================================================
# sweeper.py —— 接管「孤儿任务」：数据库说还在跑，内存里没人盯着
#
# 病根
# ----
# 视频任务的轮询线程活在进程内存里（`VideoJobManager._jobs` + 线程池）。任务状态
# 每 5 秒回写一次数据库，所以**服务商的活是真的在跑**；但这个进程一旦重启：
#
#   · 内存里的任务表清空，轮询线程断开；
#   · 数据库里那条记录仍停在 `generating`（没有任何人再去更新它）；
#   · 服务商那边其实早就出片了，片子躺在别人的 OSS 上。
#
# 用户看到的是「一直生成中」。更贵的是**这个状态没有出口**：不会超时、不会失败，
# 除非有人手动重试——而重试会重新提交一次，重复计费。
#
# 解法
# ----
# 只做一件事：**把数据库里还在跑、内存里没人盯的任务，重新挂回轮询线程**。
# 关键约束是**绝不能再 create_job**——服务商侧任务已经存在（用 external_task_id
# 指着），重提一次就是两条片子、两份钱。所以接管走 `manager.attach()`，
# 从轮询开始接着盯。
#
# 触发时机：启动时扫一次（重启后立刻接上），之后每隔一段时间扫一次
# （兜住线程意外死掉、回写失败这类零星情况）。这层是**幂等**的，重复扫无副作用。
# =====================================================================

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

#: 默认巡检间隔。60 秒足够：任务本来就每 5 秒回写一次，巡检只兜漏网的。
DEFAULT_INTERVAL_SEC = 60.0


def resume_orphans(
    *,
    store: Any = None,
    manager: Any = None,
    resolve_provider: Callable[[str], Any] | None = None,
) -> dict[str, int]:
    """接管所有孤儿任务，返回计数（接管 / 放弃 / 已在跑）。

    逐个任务的处理：
      1. 内存里已经在盯 → 跳过（幂等，不重复挂线程）；
      2. 没有 external_task_id → **判失败**。说明进程死在「提交」与「拿到任务号」
         之间，服务商那边根本没有这条任务，无从接管。明说比让它永远转圈好；
      3. 服务商配置不可用（key 被删了）→ 判失败并说明，不要静默卡住；
      4. 其余 → 挂回轮询线程。
    """
    if store is None or manager is None:
        from ..deps import store as get_store, video_manager

        store = store or get_store()
        manager = manager or video_manager()
    if resolve_provider is None:
        from .service import _resolve_video_provider

        resolve_provider = _resolve_video_provider

    counts = {"attached": 0, "already": 0, "dropped": 0}
    try:
        jobs = store.list_unfinished_video_jobs()
    except Exception as e:  # noqa: BLE001
        # 巡检不该影响启动：数据库还没就绪时（比如迁移前）直接跳过这一轮。
        log.warning("孤儿任务巡检跳过：无法读取任务表（%s）", e)
        return counts

    for job in jobs:
        if manager.is_tracked(job.id):
            counts["already"] += 1
            continue
        if not job.external_task_id:
            counts["dropped"] += 1
            _fail(store, job.id, "进程重启，且任务没有服务商任务号（提交未完成），请重新提交")
            continue
        try:
            provider = resolve_provider(job.provider)
        except Exception as e:  # noqa: BLE001
            provider, err = None, str(e)
        else:
            err = "服务商未配置或已停用"
        if provider is None:
            counts["dropped"] += 1
            _fail(store, job.id, f"无法接管任务：{err}")
            continue
        if manager.attach(job.id, provider, job.external_task_id, status=job.status or "generating"):
            counts["attached"] += 1
            log.info("[巡检] 接管任务 %s（%s，任务号 %s）",
                     job.id, job.provider, job.external_task_id)
        else:
            counts["already"] += 1
    return counts


def _fail(store: Any, job_id: str, reason: str) -> None:
    try:
        store.update_video_job(
            job_id, status="failed", error_message=reason,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("标记任务 %s 失败时出错：%s", job_id, e)


def start_sweeper(
    *,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
    run_now: bool = True,
) -> threading.Event:
    """起一个守护线程定期巡检；返回 stop 事件（测试与关闭时用）。

    幂等：进程内只会有一个线程（用模块级标记挡住重复启动）。
    """
    global _stop, _thread

    with _lock:
        if _thread is not None and _thread.is_alive():
            return _stop  # type: ignore[return-value]
        stop = threading.Event()
        _stop = stop

        def _loop() -> None:
            if run_now:
                _sweep_once()
            while not stop.wait(interval_sec):
                _sweep_once()

        _thread = threading.Thread(target=_loop, name="video-sweeper", daemon=True)
        _thread.start()
        log.info("[巡检] 孤儿任务巡检已启动（每 %.0f 秒一次）", interval_sec)
        return stop


def stop_sweeper() -> None:
    """停止巡检（应用关闭 / 测试清理）。"""
    global _thread, _stop

    with _lock:
        if _stop is not None:
            _stop.set()
        _stop = None
        _thread = None


def _sweep_once() -> None:
    try:
        counts = resume_orphans()
    except Exception as e:  # noqa: BLE001
        log.warning("[巡检] 本轮失败：%s", e)
        return
    if counts["attached"] or counts["dropped"]:
        log.info("[巡检] 接管 %d 个、放弃 %d 个、已在跑 %d 个",
                 counts["attached"], counts["dropped"], counts["already"])


_thread: threading.Thread | None = None
_stop: threading.Event | None = None
_lock = threading.Lock()
