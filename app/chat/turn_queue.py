"""Server-owned, per-conversation chat turns. Browser connections never own a run."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import Event, RLock, Thread
from typing import Any

from . import conductor


class QueueFull(Exception):
    pass


@dataclass
class Turn:
    id: str
    thread: str
    project_id: str | None
    conversation_id: str | None
    message: str
    images: list[str]
    refs: list[dict[str, Any]]
    guide: bool = False
    state: str = "queued"
    events: list[dict[str, Any]] = field(default_factory=list)
    stop: Event = field(default_factory=Event)


class TurnQueue:
    """One worker per thread; all queue transitions share one process lock."""

    def __init__(self, store: Any, llm: Any, settings: Any, runner: Any = None):
        self.store, self.llm, self.settings, self.runner = store, llm, settings, runner
        self.lock = RLock()
        self.turns: dict[str, Turn] = {}
        self.waiting: dict[str, deque[str]] = {}
        self.active: dict[str, str] = {}
        self.revisions: dict[str, int] = {}

    def _bump(self, thread: str) -> None:
        self.revisions[thread] = self.revisions.get(thread, 0) + 1

    @staticmethod
    def _key(thread: str, turn_id: str) -> str:
        return f"{thread}\x00{turn_id}"

    def submit(self, turn: Turn) -> dict[str, Any]:
        with self.lock:
            key = self._key(turn.thread, turn.id)
            existing = self.turns.get(key)
            if existing:
                if (existing.thread, existing.project_id, existing.message, existing.images,
                    existing.refs, existing.guide) != (turn.thread, turn.project_id, turn.message,
                                                       turn.images, turn.refs, turn.guide):
                    raise ValueError("重复提交编号对应不同内容")
                return self._view(existing)
            q = self.waiting.setdefault(turn.thread, deque())
            if not turn.guide and len(q) >= 5:
                raise QueueFull("等待消息已满，请选择引导当前回复")
            if turn.guide and turn.thread in self.active:
                self.turns[self.active[turn.thread]].stop.set()
            self.turns[key] = turn
            if turn.guide:
                q.appendleft(key)
            else:
                q.append(key)
            self._bump(turn.thread)
            self._start_next(turn.thread)
            return self._view(turn)

    def _start_next(self, thread: str) -> None:
        if thread in self.active or not self.waiting.get(thread):
            return
        turn_id = self.waiting[thread].popleft()
        turn = self.turns[turn_id]
        turn.state = "running"
        self.active[thread] = turn_id
        self._bump(thread)
        Thread(target=self._run, args=(turn,), daemon=True, name=f"chat-{thread}").start()

    def _run(self, turn: Turn) -> None:
        # Legacy /chat callers still share the same conversation lock.
        import time
        while not conductor.acquire_thread(turn.thread):
            time.sleep(.05)
        try:
            interrupted = False
            for event in conductor.chat_stream(
                self.store, self.llm, self.settings,
                conversation_id=turn.conversation_id, project_id=turn.project_id,
                message=turn.message, images=turn.images, refs=turn.refs,
                runner=self.runner, interrupted=turn.stop.is_set,
            ):
                if event.get("event") == "done":
                    interrupted = bool(event.get("data", {}).get("interrupted"))
                with self.lock:
                    turn.events.append(event)
            with self.lock:
                turn.state = "interrupted" if interrupted else "done"
        except Exception as exc:  # keep queue advancing after a failed turn
            with self.lock:
                turn.events.append({"event": "error", "data": {"message": str(exc)}})
                turn.state = "failed"
        finally:
            conductor.release_thread(turn.thread)
            with self.lock:
                self.active.pop(turn.thread, None)
                self._bump(turn.thread)
                self._start_next(turn.thread)

    def cancel(self, thread: str, turn_id: str) -> bool:
        with self.lock:
            key = self._key(thread, turn_id)
            turn = self.turns.get(key)
            if not turn or turn.thread != thread or turn.state != "queued":
                return False
            self.waiting[thread].remove(key)
            turn.state = "cancelled"
            self._bump(thread)
            return True

    def _view(self, turn: Turn) -> dict[str, Any]:
        q = self.waiting.get(turn.thread, ())
        key = self._key(turn.thread, turn.id)
        return {"id": turn.id, "message": turn.message, "guide": turn.guide,
                "state": turn.state, "position": list(q).index(key) + 1 if key in q else 0}

    def status(self, thread: str) -> dict[str, Any]:
        with self.lock:
            active_id = self.active.get(thread)
            return {"active": self._view(self.turns[active_id]) if active_id else None,
                    "waiting": [self._view(self.turns[tid]) for tid in self.waiting.get(thread, ())],
                    "capacity": 5, "revision": self.revisions.get(thread, 0)}

    def events(self, thread: str, turn_id: str, after: int = 0) -> dict[str, Any] | None:
        with self.lock:
            turn = self.turns.get(self._key(thread, turn_id))
            if not turn or turn.thread != thread:
                return None
            return {**self._view(turn), "events": turn.events[after:], "next": len(turn.events)}
