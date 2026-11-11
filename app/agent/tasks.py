# =====================================================================
# tasks.py —— 后台任务运行器
#
# 剧组工具（导演拆解 / 美术指导 / 摄影指导 / 定妆图）在后台线程中执行，
# 通过 SubAgentRunner 管理生命周期与进度上报（前端「Agent 任务」面板）。
# =====================================================================

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any

log = logging.getLogger(__name__)


# ---------- 任务模型 ----------


@dataclass
class AgentStep:
    """后台任务执行中的一个步骤。"""

    label: str
    status: str = "running"  # running / done / failed
    detail: str = ""


@dataclass
class SubAgentTask:
    """一个后台任务的完整状态。"""

    id: str
    name: str
    status: str = "pending"  # pending / running / done / failed
    steps: list[AgentStep] = field(default_factory=list)
    result: str | None = None
    # 任务归属的作品（2026-11-11 权限审计）：此前任务表无项目标签，
    # get_status 拿全局 list_tasks() 会把 A 作品的出片任务报进 B 作品的对话。
    project_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "project_id": self.project_id,
            "steps": [{"label": s.label, "status": s.status, "detail": s.detail} for s in self.steps],
            "result": self.result,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# ---------- 任务运行器 ----------


class SubAgentRunner:
    """管理后台任务的执行与进度追踪。

    任务在独立线程中运行，进度通过 update_step() 实时上报。
    完成后通过回调通知调用方。

    可选传入 ``storage``（Store 实例）：在任务「启动」与「结束」两个时刻
    落库一次。进度更新不落库（避免写放大）——进行中的任务本身随线程终止，
    落库的意义是让**已完成的任务历史**在进程重启后仍可查询。
    """

    def __init__(self, storage: Any = None) -> None:
        self._tasks: dict[str, SubAgentTask] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._storage = storage

    def attach_storage(self, storage: Any) -> None:
        """挂上落库存储（由 app/deps.py 在装配时调用）。

        做成可后置挂载而不是构造参数：装配顺序不再影响能否落库——
        单例先被谁拿到、存储是否可用，都不会让持久化静默失效。
        """
        if storage is not None:
            self._storage = storage

    def _persist(self, task: SubAgentTask) -> None:
        if self._storage is None:
            return
        try:
            self._storage.save_subagent_task(task.to_dict())
        except Exception as e:  # noqa: BLE001
            log.warning("后台任务 %s 落库失败：%s", task.id, e)

    def _gen_id(self) -> str:
        # 自增在锁内（2026-11-11 审计）：此前在锁外，并发 start 理论上撞 id。
        with self._lock:
            self._counter += 1
            return f"task_{self._counter:04d}"

    def start(
        self,
        name: str,
        fn: Callable[[SubAgentTask], str],
        *,
        project_id: str = "",
        on_done: Callable[[SubAgentTask], None] | None = None,
    ) -> str:
        """启动一个后台任务。返回 task_id。project_id 标记归属（进度按项目过滤用）。"""
        task_id = self._gen_id()
        task = SubAgentTask(id=task_id, name=name, status="running", project_id=project_id)
        with self._lock:
            self._tasks[task_id] = task
        self._persist(task)

        def _run() -> None:
            try:
                result = fn(task)
                task.result = result
                task.status = "done"
            except Exception as e:  # noqa: BLE001
                task.result = f"执行失败：{e}"
                task.status = "failed"
                log.warning("后台任务 %s(%s) 失败：%s", name, task_id, e)
            finally:
                task.finished_at = datetime.now(UTC)
                self._persist(task)
                if on_done:
                    on_done(task)

        thread = threading.Thread(target=_run, name=f"subagent-{task_id}", daemon=True)
        thread.start()
        return task_id

    def get(self, task_id: str) -> SubAgentTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, *, active_only: bool = False, project_id: str | None = None) -> list[SubAgentTask]:
        """列任务。project_id 给了就只列该作品的（get_status 用，防跨作品泄露）；
        顺手清理超龄的已完成任务（此前 cleanup() 无人调用，_tasks 无限增长）。"""
        self.cleanup()
        with self._lock:
            tasks = list(self._tasks.values())
        if project_id is not None:
            tasks = [t for t in tasks if t.project_id == project_id]
        if active_only:
            tasks = [t for t in tasks if t.status in ("pending", "running")]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def update_step(self, task_id: str, label: str, status: str = "running", detail: str = "") -> None:
        """更新任务进度：新增或更新一个步骤。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            # 查找已有同名步骤，更新；否则新增
            for step in task.steps:
                if step.label == label:
                    step.status = status
                    step.detail = detail
                    return
            task.steps.append(AgentStep(label=label, status=status, detail=detail))

    def cleanup(self, max_age_hours: int = 24) -> int:
        """清理超过指定小时数的已完成任务。返回清理数量。"""
        now = datetime.now(UTC)
        to_remove = []
        with self._lock:
            for tid, task in self._tasks.items():
                if task.finished_at:
                    age = (now - task.finished_at).total_seconds() / 3600
                    if age > max_age_hours:
                        to_remove.append(tid)
            for tid in to_remove:
                del self._tasks[tid]
        return len(to_remove)


# ---------- 内部辅助 ----------

_runner: SubAgentRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> SubAgentRunner:
    """公开接口：获取全局 SubAgentRunner 单例。

    落库存储由 app/deps.py 通过 ``attach_storage()`` 注入，这里不反向依赖 deps。
    """
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = SubAgentRunner()
    return _runner
