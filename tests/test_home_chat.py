# =====================================================================
# test_home_chat.py —— 首页直聊业务流程（不点「新建作品」直接开聊）
#
# 锁三件事（都是浏览器实测撞出来的断点）：
#   1. 本轮从全局线程开聊、Agent 中途建了作品 → 消息迁进作品默认对话，
#      done 回真实 conv id（否则前端第二轮带 "global" 必 404、历史孤儿）；
#   2. 没建作品的纯闲聊 → 留在全局线程，不凭空造作品对话；
#   3. 本轮 create_project 之后，同轮后续工具漏传 project_id 也能兜到新作品。
# =====================================================================

from __future__ import annotations

from langchain_core.messages import AIMessage

from app.chat.conductor import GLOBAL_THREAD, _finalize_turn
from app.chat.loop import AgentLoop


class _FakeCollector(dict):
    pass


def _mk_project(store, title="暴雨夜"):
    p = store.create_project(title=title, adaptation_type="short_drama",
                             language="zh-CN", raw_text="暴雨夜，修车铺学徒发现一辆老轿车。")
    return p


# ---------- 1) 迁移：建了作品 → 消息进作品默认对话 ----------


def test_global_turn_with_new_project_migrates(store, llm, settings):
    p = _mk_project(store)
    # 模拟流式路径：用户消息在流开始时已落在全局线程，收尾只补 assistant
    store.save_chat_message(thread_id=GLOBAL_THREAD, role="user", content="帮我做成短剧")
    collector = {"project_id": p.id, "payloads": []}
    out = _finalize_turn(
        store, collector, None, None, "帮我做成短剧", None,
        "已创建作品", [], save_user=False,
    )
    # done 回真实 conv id，不是 "global"
    assert out["thread_id"] != GLOBAL_THREAD
    conv = store.get_conversation(out["thread_id"])
    assert conv.project_id == p.id
    # 全局线程清空，作品对话里能看到完整来路（用户 + 助手）
    assert store.list_chat_messages(GLOBAL_THREAD) == []
    moved = store.list_chat_messages(conv.id)
    assert [m.role for m in moved] == ["user", "assistant"]
    assert moved[0].content == "帮我做成短剧"


def test_global_turn_without_project_stays(store, llm, settings):
    """纯闲聊没聊出作品：留在全局线程，不凭空造作品对话。"""
    store.save_chat_message(thread_id=GLOBAL_THREAD, role="user", content="你好")
    collector = {"project_id": None, "payloads": []}
    out = _finalize_turn(
        store, collector, None, None, "你好", None,
        "你好，我在", [], save_user=False,
    )
    assert out["thread_id"] == GLOBAL_THREAD
    assert len(store.list_chat_messages(GLOBAL_THREAD)) == 2


def test_project_conversation_turn_does_not_move(store, llm, settings):
    """已经挂在作品对话里的轮次：不触发迁移（conversation_id 非空）。"""
    p = _mk_project(store)
    conv = store.ensure_default_conversation(p.id)
    store.save_chat_message(thread_id=conv.id, role="user", content="改一下对白")
    collector = {"project_id": p.id, "payloads": []}
    out = _finalize_turn(
        store, collector, conv.id, p.id, "改一下对白", None,
        "已改编", [], save_user=False,
    )
    assert out["thread_id"] == conv.id
    assert len(store.list_chat_messages(conv.id)) == 2


# ---------- 3) 同轮兜底：create_project 后后续工具漏传 project_id ----------


class _PidTool:
    """带 project_id 参数的工具；记录每次收到的 args。"""

    name = "needs_pid"
    description = "需要 project_id"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, args):  # noqa: ANN001
        self.calls.append(dict(args or {}))
        return "ok"


class _FakeModel:
    def __init__(self, script) -> None:
        self.script = list(script)

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):
        return self.script.pop(0) if self.script else AIMessage(content="（脚本用尽）")


class _FakeLLM:
    available = True

    def __init__(self, model):
        self._model = model

    def chat(self):
        return self._model

    def chat_multimodal(self):
        return self._model


def test_loop_falls_back_to_collector_project_id():
    """本轮 create_project 写进 collector 后，同轮后续工具漏传的 project_id 兜到它。"""
    tool = _PidTool()
    # 模型第一次调工具时 collector 还没有作品（模拟 create_project 之前的状态由外部填），
    # 这里直接预置 collector 有 project_id，验证兜底读的是动态 collector 而非构造时死值。
    model = _FakeModel([
        AIMessage(content="", tool_calls=[
            {"name": "needs_pid", "args": {"project_id": ""}, "id": "c1"},
        ]),
        AIMessage(content="完成"),
    ])
    loop = AgentLoop(_FakeLLM(model), [tool], current_project_id="")
    collector: dict = {"project_id": "proj_new"}
    loop.collector = collector

    from langchain_core.messages import ToolMessage  # noqa: F401

    loop.run([{"role": "user", "content": "帮我做成短剧"}])
    # 漏传（空串）被兜成 collector 里的新作品 id
    assert tool.calls and tool.calls[0].get("project_id") == "proj_new"


def test_loop_collector_none_falls_back_to_static():
    """没接 collector 时退回构造时的 current_project_id（向后兼容）。"""
    tool = _PidTool()
    model = _FakeModel([
        AIMessage(content="", tool_calls=[
            {"name": "needs_pid", "args": {"project_id": ""}, "id": "c1"},
        ]),
        AIMessage(content="完成"),
    ])
    loop = AgentLoop(_FakeLLM(model), [tool], current_project_id="proj_static")
    loop.run([{"role": "user", "content": "x"}])
    assert tool.calls[0].get("project_id") == "proj_static"
