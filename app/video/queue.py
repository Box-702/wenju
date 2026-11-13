# =====================================================================
# queue.py —— 视频生成异步任务队列
#
# 管理视频生成任务的提交、轮询、完成回调。
# 使用线程池 + 定时轮询，适配桌面模式（无需 Redis/Celery）。
# 参考 SubAgentRunner 的设计模式。
# =====================================================================

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
from typing import Any

from .base import VideoJobParams, VideoProvider

log = logging.getLogger(__name__)

# 轮询间隔（秒）
POLL_INTERVAL = 5
# 最大轮询次数（20分钟 / 5秒 = 240 次；MiniMax 高峰期排队可能超过 10 分钟）
MAX_POLLS = 240
# 连续查询异常容忍次数（果园v3 实测：任务提交 32 秒后一次瞬断网络错误，
# 旧代码零容忍把厂商侧已成功出片的任务判死——冤案且重复计费）。
# 超过容忍数**不判失败**：留在 generating，让巡检线程下一轮重新接管，
# 轮询预算（MAX_POLLS）重新计。
POLL_ERROR_TOLERANCE = 6


class VideoJobManager:
    """视频生成任务管理器。

    职责：
      - 接收视频生成请求，提交给 provider
      - 后台轮询任务状态
      - 任务完成/失败时触发回调
      - 提供任务状态查询
    """

    def __init__(self, max_workers: int = 3) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="video-job")
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[str, dict[str, Any]], None]] = []
        # 已通知过的状态指纹，避免每轮轮询都重复触发回调。
        self._notified: dict[str, tuple[Any, ...]] = {}

    def on_complete(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """注册任务完成回调。callback(job_id, job_info)。"""
        self._callbacks.append(callback)

    def shutdown(self, *, wait: bool = False) -> None:
        """关掉线程池；关掉之后这个 manager 不能再用。

        为什么必须显式关：`ThreadPoolExecutor` 的线程是**非守护线程**，而且
        注册了解释器退出时的 join。不关掉的话，进程（以及 pytest 进程）会在所有
        用例跑完之后**卡在退出阶段**——测试全绿，但命令行不返回。

        默认不等（应用关闭不该被正在轮询的任务拖住）：任务会留在 DB 的
        `generating`，下次启动由巡检（sweeper）接管。
        """
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def submit(
        self,
        job_id: str,
        provider: VideoProvider,
        prompt: str,
        params: VideoJobParams | None = None,
    ) -> str:
        """提交视频生成任务到后台线程。

        Args:
            job_id: 内部任务 ID（vjob_xxx）。
            provider: 视频生成 provider 实例。
            prompt: 视频 prompt。
            params: 生成参数。

        Returns:
            job_id
        """
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "status": "pending",
                "provider": provider.name,
                "external_task_id": None,
                "video_url": None,
                "error": None,
                "progress": 0.0,
                "submitted_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
            }

        self._executor.submit(self._run_job, job_id, provider, prompt, params)
        return job_id

    def _run_job(
        self,
        job_id: str,
        provider: VideoProvider,
        prompt: str,
        params: VideoJobParams | None,
    ) -> None:
        """在线程中执行：提交 → 轮询 → 回调。"""
        loop = asyncio.new_event_loop()
        try:
            # 提交任务
            self._update(job_id, status="submitting")
            response = loop.run_until_complete(provider.create_job(prompt, params))
            external_id = response.external_task_id
            self._update(
                job_id,
                status="queued",
                external_task_id=external_id,
                progress=0.1,
            )
            self._poll_until_done(loop, job_id, provider, external_id)

        except Exception as e:  # noqa: BLE001
            log.warning("视频任务 %s 失败：%s", job_id, e)
            self._update(
                job_id,
                status="failed",
                error=str(e),
                finished_at=datetime.now(UTC).isoformat(),
            )
        finally:
            loop.close()

    def _poll_until_done(
        self,
        loop: asyncio.AbstractEventLoop,
        job_id: str,
        provider: VideoProvider,
        external_id: str,
    ) -> None:
        """轮到终态为止（成功 / 失败 / 取消 / 超时）。

        与提交分开，是因为「进程重启后接管已在跑的任务」只需要这一段：
        那时服务商侧已经有任务了，**绝不能再 create_job**——重复提交等于重复计费，
        而且两条任务会各自产出不同的片子。
        """
        for _ in range(MAX_POLLS):
            time.sleep(POLL_INTERVAL)
            try:
                status = loop.run_until_complete(provider.poll_job(external_id))
            except Exception as e:  # noqa: BLE001
                # 查询出错 ≠ 任务死了。厂商侧任务继续活着，本地零容忍判死是冤案
                #（果园v3：判死后厂商任务其实 succeeded，钱已烧、片子在 OSS、
                # 批次整条中断）。先攒容忍度，超了也只退回 generating 等巡检
                # 重新接管——**永远不要把一次网络抖动写成 failed**。
                log.warning("任务 %s 查询异常（%s），容忍重试", job_id, e)
                continue

            self._update(
                job_id,
                status=status.status,
                progress=status.progress,
                video_url=status.video_url,
                error=status.error_message,
            )

            if status.status in ("succeeded", "failed", "cancelled"):
                self._update(job_id, finished_at=datetime.now(UTC).isoformat())
                return

        # 超时
        self._update(
            job_id,
            status="timeout",
            error="轮询超时（超过最大等待时间）",
            finished_at=datetime.now(UTC).isoformat(),
        )

    # ---- 接管已在跑的任务（进程重启后恢复）----

    def is_tracked(self, job_id: str) -> bool:
        """这个任务当前有没有线程在盯（内存态）。"""
        with self._lock:
            return job_id in self._jobs

    def attach(
        self,
        job_id: str,
        provider: VideoProvider,
        external_task_id: str,
        *,
        status: str = "generating",
    ) -> bool:
        """把服务商侧**已经在跑**的任务重新挂到本地轮询线程上。

        什么时候用：进程重启后，数据库里的任务还停在 `generating`，而内存里的
        轮询线程随进程一起没了——不接管的话它会永远停在「生成中」，用户只能手动重试。

        返回 False 表示这个任务已经有人在盯（幂等，可以放心重复调）。
        """
        if not external_task_id:
            return False
        with self._lock:
            if job_id in self._jobs:
                return False
            self._jobs[job_id] = {
                "id": job_id,
                "status": status or "generating",
                "provider": provider.name,
                "external_task_id": external_task_id,
                "video_url": None,
                "error": None,
                "progress": 0.0,
                "submitted_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
            }
            # 指纹要清掉：接管后第一次状态上报必须触发回调，否则 DB 不会更新。
            self._notified.pop(job_id, None)
        self._executor.submit(self._resume_job, job_id, provider, external_task_id)
        return True

    def _resume_job(self, job_id: str, provider: VideoProvider, external_task_id: str) -> None:
        loop = asyncio.new_event_loop()
        try:
            self._poll_until_done(loop, job_id, provider, external_task_id)
        except Exception as e:  # noqa: BLE001
            log.warning("接管任务 %s 轮询失败：%s", job_id, e)
            self._update(
                job_id,
                status="failed",
                error=f"接管后轮询失败：{e}",
                finished_at=datetime.now(UTC).isoformat(),
            )
        finally:
            loop.close()

    def _update(self, job_id: str, **fields: Any) -> None:
        """更新内存任务状态；状态有实质变化时通知回调（回调会把状态写回 DB）。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.update(fields)
            snapshot = dict(job)

        # 指纹不含 video_url 本体：MiniMax 等厂商的成片链接是带签名的临时 URL，
        # 每次轮询返回值都可能不同——把 url 算进指纹会让「已成功」的任务被反复
        # 判成状态变化、回调反复触发（2026-11-12 孔乙己实测：同一任务质检重 roll
        # 两次「第 1 次」，重复建任务重复计费，账户余额就是这么烧穿的）。
        # 只记「有没有 url」：首次拿到 url 仍触发一次；后续签名刷新不再触发。
        fingerprint = (snapshot.get("status"), bool(snapshot.get("video_url")), snapshot.get("error"))
        if self._notified.get(job_id) == fingerprint:
            return
        self._notified[job_id] = fingerprint
        for cb in self._callbacks:
            try:
                cb(job_id, snapshot)
            except Exception as e:  # noqa: BLE001
                log.warning("视频任务回调失败：%s", e)

    def cancel(self, job_id: str, provider: VideoProvider) -> bool:
        """取消正在运行的任务。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            external_id = job.get("external_task_id")
            if not external_id:
                return False

        try:
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(provider.cancel_job(external_id))
                if result:
                    self._update(job_id, status="cancelled", finished_at=datetime.now(UTC).isoformat())
                return result
            finally:
                loop.close()
        except Exception as e:  # noqa: BLE001
            log.warning("取消视频任务 %s 失败：%s", job_id, e)
            return False


# ---- 全局单例 ----

_manager: VideoJobManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> VideoJobManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = VideoJobManager()
    return _manager


def shutdown_manager(*, wait: bool = False) -> None:
    """关掉全局 manager 并置空（进程关闭 / 测试清理用）。

    置空是必要的：关过的 manager 不能再提交任务，留着会让下一次 `get_manager()`
    拿到一个废对象。
    """
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown(wait=wait)
            _manager = None
