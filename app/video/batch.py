# =====================================================================
# batch.py —— 分镜批量生成编排（自动 / 审批两种模式）
#
# 两种模式：
#   - auto（自动）：一次跑完全部分镜——每镜成功后自动提交下一镜；
#   - approval（审批）：每跑完一个镜头暂停，等用户预览后下命令
#     （approve 继续 / reroll 重跑当前镜 / abort 终止）。
#
# 设计约束：
#   - 串行：同一时刻只有一镜在生成。跨镜接力（reference_videos）依赖
#     前镜成片 URL，并行生成拿不到锚，还会放大模型的不一致漂移；
#   - 事件驱动：批次推进挂在 VideoJobManager 的完成回调上，不占线程；
#   - 状态在内存（BatchManager 单例）：进度可随时从 video_jobs 表对账，
#     进程重启后未完成的批次作废，用户手动重新发起即可。
# =====================================================================

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, Callable

log = logging.getLogger(__name__)

# 批次状态机：running → (awaiting_approval ⇄ running)* → done / aborted / failed
MODES = ("auto", "approval")


@dataclass
class Batch:
    """一个批次的完整状态。"""

    id: str
    project_id: str
    mode: str                      # auto | approval
    shot_ids: list[str]            # 按生成顺序
    current_index: int = 0         # 当前正在生成（或等待审批）的镜头下标
    status: str = "running"        # running / awaiting_approval / done / aborted / failed
    results: dict[str, str] = field(default_factory=dict)   # shot_id -> video_url
    job_id: str | None = None      # 当前在跑的任务
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    submit_fn: Callable[[str], str | None] | None = field(
        default=None, repr=False, compare=False
    )  # 启动时注入的提交回调（auto 模式推进下一镜用），不序列化

    @property
    def current_shot_id(self) -> str | None:
        if 0 <= self.current_index < len(self.shot_ids):
            return self.shot_ids[self.current_index]
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "mode": self.mode,
            "status": self.status,
            "current_index": self.current_index,
            "total": len(self.shot_ids),
            "current_shot_id": self.current_shot_id,
            "shot_ids": self.shot_ids,
            "results": self.results,
            "job_id": self.job_id,
            "error": self.error,
            "created_at": self.created_at,
        }


class BatchManager:
    """批次编排器：串行推进 + 审批暂停。提交动作由注入的 submit 回调执行。"""

    def __init__(self) -> None:
        self._batches: dict[str, Batch] = {}
        self._lock = threading.Lock()

    # ---- 查询 ----

    def get(self, batch_id: str) -> Batch | None:
        with self._lock:
            return self._batches.get(batch_id)

    def latest_for_project(self, project_id: str) -> Batch | None:
        with self._lock:
            found = [b for b in self._batches.values() if b.project_id == project_id]
        return max(found, key=lambda b: b.created_at) if found else None

    def running_for_project(self, project_id: str) -> Batch | None:
        """该项目当前还在推进的批次（没到终态）。

        同一项目同时跑两个批次会重复提交同一批镜头、重复计费，
        而且两个批次会互相抢「当前镜头」的完成回调（`_find_running` 按
        current_shot_id 匹配，命中第一个就返回）。提交新批次前先查这个。
        """
        with self._lock:
            found = [
                b for b in self._batches.values()
                if b.project_id == project_id
                and b.status in ("running", "awaiting_approval")
            ]
        return max(found, key=lambda b: b.created_at) if found else None

    # ---- 启动 ----

    def start(
        self,
        project_id: str,
        shot_ids: list[str],
        mode: str,
        submit: Callable[[str], str | None],
    ) -> Batch:
        """创建批次并提交第一镜。submit(shot_id) 返回 job_id（失败返回 None）。

        submit 由调用方注入（API 层复用 _submit_video_job 的完整链路：
        自动锚回填 → 落库 → 投递队列），本模块不感知存储细节。
        """
        if mode not in MODES:
            raise ValueError(f"未知生成模式：{mode}（可选 {MODES}）")
        if not shot_ids:
            raise ValueError("批次没有镜头")
        batch = Batch(
            id=f"batch_{uuid.uuid4().hex[:10]}",
            project_id=project_id,
            mode=mode,
            shot_ids=list(shot_ids),
        )
        job_id = submit(batch.shot_ids[0])
        if job_id is None:
            batch.status = "failed"
            batch.error = "第一镜提交失败"
        else:
            batch.job_id = job_id
        batch.submit_fn = submit
        with self._lock:
            self._batches[batch.id] = batch
        log.info("批次启动：%s（%s，%d 镜）", batch.id, mode, len(shot_ids))
        return batch

    # ---- 事件驱动推进 ----

    def on_job_finished(self, project_id: str, shot_id: str, job: Any) -> Batch | None:
        """任务完成回调入口（成功或失败都调）。

        返回受影响的批次（供调用方记录日志/通知），无关联批次返回 None。
        """
        submit_shot: str | None = None
        with self._lock:
            batch = self._find_running(project_id, shot_id)
            if batch is None:
                return None
            status = str(getattr(job, "status", "") or "")
            video_url = getattr(job, "video_url", None)

            if status != "succeeded" or not video_url:
                batch.status = "failed"
                batch.error = f"镜头 {shot_id} 生成失败：{getattr(job, 'error_message', None) or status}"
                log.warning("批次 %s 失败：%s", batch.id, batch.error)
                return batch

            batch.results[shot_id] = video_url
            if batch.mode == "approval":
                batch.status = "awaiting_approval"
                log.info("批次 %s 等待审批：%s（%d/%d）",
                         batch.id, shot_id, batch.current_index + 1, len(batch.shot_ids))
            else:
                self._advance(batch)
                if batch.status == "running":
                    # 自动模式：锁外提交下一镜（串行，接力依赖本镜成片 URL）。
                    submit_shot = batch.current_shot_id

        if submit_shot is not None:
            job_id = (batch.submit_fn or (lambda _sid: None))(submit_shot)
            with self._lock:
                if job_id is None:
                    batch.status = "failed"
                    batch.error = f"下一镜 {submit_shot} 提交失败"
                else:
                    batch.job_id = job_id
        return batch

    def _find_running(self, project_id: str, shot_id: str) -> Batch | None:
        for b in self._batches.values():
            if (b.project_id == project_id and b.status in ("running", "awaiting_approval")
                    and b.current_shot_id == shot_id):
                return b
        return None

    def _advance(self, batch: Batch) -> None:
        """推进到下一镜（在锁内调用）。最后一镜成功 → done。"""
        batch.current_index += 1
        batch.job_id = None
        if batch.current_index >= len(batch.shot_ids):
            batch.status = "done"
            log.info("批次 %s 完成（%d 镜）", batch.id, len(batch.shot_ids))

    # ---- 用户命令 ----

    def approve(self, batch_id: str, submit: Callable[[str], str | None]) -> Batch:
        """审批通过：提交下一镜（auto 模式推进也走这里，由 on_job_finished 内部处理）。"""
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise KeyError(f"批次不存在：{batch_id}")
            if batch.status != "awaiting_approval":
                raise ValueError(f"批次不在等待审批状态：{batch.status}")
            if batch.current_index + 1 >= len(batch.shot_ids) and batch.current_shot_id in batch.results:
                batch.status = "done"
                return batch
            batch.status = "running"
            batch.current_index += 1
            batch.job_id = None
            shot_id = batch.current_shot_id
        job_id = submit(shot_id)
        with self._lock:
            if job_id is None:
                batch.status = "failed"
                batch.error = f"镜头 {shot_id} 提交失败"
            else:
                batch.job_id = job_id
        return batch

    def reroll(self, batch_id: str, submit: Callable[[str], str | None]) -> Batch:
        """重跑当前镜（审批模式预览不满意时）。"""
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise KeyError(f"批次不存在：{batch_id}")
            if batch.status != "awaiting_approval":
                raise ValueError(f"批次不在等待审批状态：{batch.status}")
            batch.status = "running"
            shot_id = batch.current_shot_id
        job_id = submit(shot_id)
        with self._lock:
            if job_id is None:
                batch.status = "failed"
                batch.error = f"镜头 {shot_id} 重跑提交失败"
            else:
                batch.job_id = job_id
        return batch

    def abort(self, batch_id: str) -> Batch:
        """终止批次（已生成的镜头不受影响）。"""
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise KeyError(f"批次不存在：{batch_id}")
            if batch.status not in ("done", "aborted"):
                batch.status = "aborted"
        return batch


# ---- 全局单例 ----

_manager: BatchManager | None = None


def get_batch_manager() -> BatchManager:
    global _manager
    if _manager is None:
        _manager = BatchManager()
    return _manager
