# =====================================================================
# test_chat_mutex.py —— 会话互斥：一个对话同时只跑一轮
#
# 并发两轮同一会话的后果是消息交错、滚动摘要互相覆盖、工具轨迹搅在一起
# （用户看到的是一团乱，比直接报错糟得多）。这组测试锁住：
#   1. 忙时拒绝（非流式 / 流式都 409）；
#   2. 正常结束 / 客户端断开后锁被释放（三个出口都有覆盖）；
#   3. 审阅动作（resume）走确定性路径，不占互斥位。
# =====================================================================

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
from app.agent.tasks import SubAgentRunner
from app.chat import conductor
from app.main import create_app


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    monkeypatch.setattr(api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store))
    return TestClient(create_app())


def _conversation(client, sample_text) -> tuple[str, str]:
    """建一个项目并跑一轮，拿回 (project_id, conversation thread_id)。"""
    resp = client.post("/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"})
    assert resp.status_code == 200, resp.text
    pid = resp.json()["id"]
    first = client.post("/api/chat", json={"project_id": pid, "message": "你好"})
    assert first.status_code == 200, first.text
    return pid, first.json()["thread_id"]


def _drain_sse(client, pid, conv_id, message):
    with client.stream(
        "POST", "/api/chat/stream",
        json={"project_id": pid, "conversation_id": conv_id, "message": message},
    ) as resp:
        names = []
        for line in resp.iter_lines():
            if line.startswith("event: "):
                names.append(line[len("event: "):].strip())
        return resp, names


# ---------- 忙时拒绝 ----------


def test_busy_conversation_rejected_nonstream(client, sample_text):
    pid, conv = _conversation(client, sample_text)
    assert conductor.acquire_thread(conv)
    try:
        resp = client.post(
            "/api/chat", json={"project_id": pid, "conversation_id": conv, "message": "再发一条"}
        )
        assert resp.status_code == 409, resp.text
        assert "在运行中" in resp.json()["detail"]
    finally:
        conductor.release_thread(conv)
    # 释放后同样的请求应被接受（降级路径也是完整一轮）
    resp = client.post("/api/chat", json={"project_id": pid, "conversation_id": conv, "message": "再发一条"})
    assert resp.status_code == 200, resp.text


def test_busy_conversation_rejected_stream(client, sample_text):
    pid, conv = _conversation(client, sample_text)
    assert conductor.acquire_thread(conv)
    try:
        resp = client.post(
            "/api/chat/stream",
            json={"project_id": pid, "conversation_id": conv, "message": "流式也挡"},
        )
        assert resp.status_code == 409, resp.text
    finally:
        conductor.release_thread(conv)


def test_resume_bypasses_mutex(client, sample_text):
    """审阅动作走确定性路径、不碰 LLM，不占互斥位也不被它挡。"""
    pid, conv = _conversation(client, sample_text)
    assert conductor.acquire_thread(conv)
    try:
        resp = client.post(
            "/api/chat",
            json={
                "project_id": pid, "conversation_id": conv, "message": "",
                "meta": {"intent": "resume", "run_id": "run_不存在的", "action": "reject"},
            },
        )
        assert resp.status_code != 409, resp.text
    finally:
        conductor.release_thread(conv)


# ---------- 释放出口 ----------


def test_stream_releases_after_normal_completion(client, sample_text):
    pid, conv = _conversation(client, sample_text)
    resp, names = _drain_sse(client, pid, conv, "看看这部小说")
    assert resp.status_code == 200
    assert "done" in names
    # 流正常耗尽后锁应已释放：能再次占到
    assert conductor.acquire_thread(conv)
    conductor.release_thread(conv)


def test_stream_releases_after_client_disconnect(client, sample_text):
    """客户端中途断开（切走会话）：互斥位必须释放，不能把会话永久锁死。"""
    pid, conv = _conversation(client, sample_text)

    def _start_then_quit():
        with client.stream(
            "POST", "/api/chat/stream",
            json={"project_id": pid, "conversation_id": conv, "message": "长一点的要求"},
        ) as r:
            for _ in r.iter_lines():
                break  # 读一行就走：退出 with 即断开

    _start_then_quit()
    # 断开后允许短暂异步收尾（生成器 close 是同步的，这里轮询几次兜底）
    acquired = False
    for _ in range(50):
        acquired = conductor.acquire_thread(conv)
        if acquired:
            break
        threading.Event().wait(0.02)
    assert acquired, "客户端断开后会话互斥位没有释放——该会话被永久锁死"
    conductor.release_thread(conv)


def test_nonstream_round_releases(client, sample_text):
    """一轮普通对话结束后锁自动释放：连着两轮都能被接受。"""
    pid, conv = _conversation(client, sample_text)
    r1 = client.post("/api/chat", json={"project_id": pid, "conversation_id": conv, "message": "第一轮"})
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/chat", json={"project_id": pid, "conversation_id": conv, "message": "第二轮"})
    assert r2.status_code == 200, r2.text


# ---------- 原语本身 ----------


def test_concurrent_acquire_exactly_one_wins():
    """两个线程同时占同一条线程：恰有一个成功（互斥是原子的）。"""
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def worker() -> None:
        barrier.wait()
        results.append(conductor.acquire_thread("t_concurrent"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    conductor.release_thread("t_concurrent")
    # 释放后可以再占
    assert conductor.acquire_thread("t_concurrent")
    conductor.release_thread("t_concurrent")
