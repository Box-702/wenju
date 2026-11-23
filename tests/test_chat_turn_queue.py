from __future__ import annotations

import time
from threading import Event

import pytest
from langchain_core.messages import AIMessage

from app.chat.loop import AgentLoop, user_message
from app.chat.turn_queue import QueueFull, Turn, TurnQueue


def _wait(predicate):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("worker did not reach expected state")


def test_queue_fifo_capacity_withdraw_idempotency_and_guide(monkeypatch):
    entered = Event()
    release = Event()
    started = []

    def fake_stream(_store, _llm, _settings, *, message, interrupted, **_kwargs):
        started.append(message)
        if message == "first":
            entered.set()
            release.wait(2)
            yield {"event": "done", "data": {"interrupted": interrupted()}}
        else:
            yield {"event": "done", "data": {"reply": message}}

    monkeypatch.setattr("app.chat.turn_queue.conductor.chat_stream", fake_stream)
    queue = TurnQueue(None, None, None)

    def turn(n, message, *, guide=False, thread="a"):
        return Turn(str(n), thread, None, None, message, [], [], guide)

    queue.submit(turn(0, "first"))
    assert entered.wait(2)
    for i in range(1, 6):
        assert queue.submit(turn(i, str(i)))["position"] == i
    assert queue.submit(turn(5, "5"))["position"] == 5
    with pytest.raises(QueueFull):
        queue.submit(turn(6, "six"))
    assert queue.cancel("a", "2")
    assert not queue.cancel("a", "0")
    assert [t["position"] for t in queue.status("a")["waiting"]] == [1, 2, 3, 4]
    queue.submit(turn(7, "guide", guide=True))
    assert queue.turns[queue._key("a", "0")].stop.is_set()
    assert queue.status("a")["waiting"][0]["message"] == "guide"
    queue.submit(turn(8, "other thread", thread="b"))
    _wait(lambda: queue.turns[queue._key("b", "8")].state == "done")
    release.set()
    _wait(lambda: not queue.status("a")["active"])
    assert started == ["first", "other thread", "guide", "1", "3", "4", "5"]
    assert queue.events("a", "0")["events"][-1]["data"]["interrupted"]


def test_interruption_stops_before_next_tool():
    stop = Event()
    calls = []

    class Tool:
        name = "one"
        description = "one"

        def invoke(self, _args):
            calls.append("one")
            stop.set()
            return "done"

    class Model:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            return AIMessage(content="", tool_calls=[
                {"name": "one", "args": {}, "id": "a"},
                {"name": "one", "args": {}, "id": "b"},
            ])

    class LLM:
        available = True

        def chat(self):
            return Model()

    result = AgentLoop(LLM(), [Tool()], interrupted=stop.is_set).run([user_message("go")])
    assert calls == ["one"]
    assert any(event["type"] == "_interrupted" for event in result.events)


def test_fast_turn_changes_revision_even_when_poll_misses_running(monkeypatch):
    monkeypatch.setattr("app.chat.turn_queue.conductor.chat_stream",
                        lambda *_args, **_kwargs: iter([{"event": "done", "data": {"reply": "ok"}}]))
    queue = TurnQueue(None, None, None)
    before = queue.status("a")["revision"]
    queue.submit(Turn("same-id", "a", None, None, "one", [], []))
    _wait(lambda: queue.status("a")["active"] is None)
    assert queue.status("a")["revision"] > before
    # Client request ids are scoped to the conversation, including another window.
    queue.submit(Turn("same-id", "b", None, None, "two", [], []))
    _wait(lambda: queue.status("b")["active"] is None)
