# =====================================================================
# test_loop.py —— 对话内核（AgentLoop）
#
# 内核是手写的 while 循环，最容易出错的是这几处：
#   - 流式下工具调用是**分片**到达的，聚合后必须还原成完整参数；
#   - 工具抛异常不能炸掉整轮，要包成文本回给模型；
#   - 工具轮数必须收敛（上限），否则会无限打转；
#   - 没配模型时要给友好提示而不是抛异常。
#
# 全部用假模型测，不触网、不需要 key。
# =====================================================================

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from app.chat.loop import (
    MAX_TOOL_STEPS,
    AgentLoop,
    assistant_message,
    system_message,
    user_message,
)


class FakeTool:
    name = "echo"
    description = "回声"

    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[dict] = []
        self.boom = boom

    def invoke(self, args):  # noqa: ANN001
        self.calls.append(dict(args))
        if self.boom:
            raise RuntimeError("工具炸了")
        return f"echo:{args.get('x')}"


class TimeoutTool(FakeTool):
    def invoke(self, args):
        self.calls.append(dict(args))
        raise TimeoutError("test tool timed out")


class FakeModel:
    """按脚本回放的假模型：每一步返回一个 AIMessage。"""

    def __init__(self, script, *, stream: bool = True) -> None:
        self.script = list(script)
        self.calls: list[list] = []
        self.streaming = stream

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        if not self.script:
            return AIMessage(content="（脚本用尽）")
        return self.script.pop(0)

    def stream(self, messages):
        self.calls.append(list(messages))
        msg = self.script.pop(0) if self.script else AIMessage(content="（脚本用尽）")
        if isinstance(msg, AIMessage) and msg.tool_calls:
            # 真实 ChatOpenAI 把工具调用拆成分片；这里按一半切一次参数，
            # 专门验证「分片聚合」这条路径。
            for call in msg.tool_calls:
                import json

                raw = json.dumps(call.get("args") or {}, ensure_ascii=False)
                half = max(1, len(raw) // 2)
                yield AIMessageChunk(content="", tool_call_chunks=[
                    {"name": call.get("name"), "args": raw[:half], "id": call.get("id"), "index": 0}
                ])
                yield AIMessageChunk(content="", tool_call_chunks=[
                    {"args": raw[half:], "index": 0}
                ])
            return
        for ch in str(msg.content or ""):
            yield AIMessageChunk(content=ch)


class FakeLLM:
    def __init__(self, model: FakeModel | None, *, available: bool = True) -> None:
        self.available = available
        self._model = model
        self.multimodal_calls = 0

    def chat(self):
        return self._model

    def chat_multimodal(self):
        self.multimodal_calls += 1
        return self._model


def _loop(model, tools=None, **kwargs):
    return AgentLoop(FakeLLM(model), tools if tools is not None else [FakeTool()], **kwargs)


# ---------- 基本协议 ----------


def test_message_helpers_are_openai_shaped():
    assert system_message("s") == {"role": "system", "content": "s"}
    assert user_message("u") == {"role": "user", "content": "u"}
    assert assistant_message("a") == {"role": "assistant", "content": "a"}


def test_tool_round_trip_feeds_result_back_to_model():
    """一轮工具调用：模型 → 工具 → 模型，工具结果以 role=tool 回到上下文。"""
    model = FakeModel([
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {"x": 42}, "id": "c1"}]),
        AIMessage(content="答案是 echo:42"),
    ], stream=False)
    tool = FakeTool()
    messages = [system_message("s"), user_message("hi")]

    result = AgentLoop(FakeLLM(model), [tool]).run(messages)

    assert result.text == "答案是 echo:42"
    assert [e["type"] for e in result.events] == ["tool_call", "tool_result"]
    assert tool.calls == [{"x": 42}]
    # 第二次调模型时，上下文里已经带着工具结果
    tool_msgs = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert tool_msgs and tool_msgs[0].content == "echo:42"


def test_streaming_aggregates_split_tool_call_arguments():
    """流式下工具参数是分片到达的——聚合后必须还原成完整 dict。

    这是换掉 ToolNode 之后最容易坏的地方：拿不到完整参数，工具就会带着半个
    JSON 去执行。
    """
    model = FakeModel([
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {"x": 42}, "id": "c1"}]),
        AIMessage(content="ok"),
    ], stream=True)
    tool = FakeTool()
    result = _loop(model, [tool]).run([user_message("hi")])
    assert tool.calls == [{"x": 42}], tool.calls
    assert result.text == "ok"


def test_stream_yields_token_events_in_order():
    model = FakeModel([AIMessage(content="你好")], stream=True)
    events = list(_loop(model).stream([user_message("hi")]))
    tokens = "".join(e.data["delta"] for e in events if e.type == "token")
    assert tokens == "你好"
    assert events[-1].type == "_complete"


def test_no_tool_calls_ends_loop_immediately():
    model = FakeModel([AIMessage(content="直接回答")], stream=False)
    result = _loop(model).run([user_message("hi")])
    assert result.text == "直接回答"
    assert result.events == []


# ---------- 收敛与容错 ----------


def test_tool_loop_is_bounded():
    """模型不停调工具时必须收敛，不能无限打转。"""
    script = [
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {"x": i}, "id": f"c{i}"}])
        for i in range(MAX_TOOL_STEPS + 5)
    ]
    model = FakeModel(script, stream=False)
    result = _loop(model, max_steps=3).run([user_message("hi")])
    assert result.truncated is True
    assert "工具调用有点多" in result.text
    # 最多 3 轮工具
    assert len(result.events) == 3 * 2


def test_tool_exception_becomes_text_not_crash():
    """工具抛异常不能炸掉整轮——包成文本回给模型，让它自己补救。"""
    model = FakeModel([
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {"x": 1}, "id": "c1"}]),
        AIMessage(content="工具坏了，我换个办法"),
    ], stream=False)
    tool = FakeTool(boom=True)
    result = _loop(model, [tool]).run([user_message("hi")])
    assert "工具执行失败" in result.events[1]["summary"]
    assert result.text == "工具坏了，我换个办法"


def test_unknown_tool_returns_actionable_message():
    model = FakeModel([
        AIMessage(content="", tool_calls=[{"name": "nope", "args": {}, "id": "c1"}]),
        AIMessage(content="好的"),
    ], stream=False)
    result = _loop(model).run([user_message("hi")])
    assert "未知工具" in result.events[1]["summary"]


def test_skill_gated_tool_is_hidden_until_skill_loaded():
    tool = FakeTool()
    tool.name = "generate_script"
    tool.metadata = {"required_skill": "novel-serial"}
    loop = _loop(FakeModel([]), [tool])
    loop.collector = {"loaded_skills": []}
    assert loop._available_tools() == []
    assert "先调用 load_skill" in loop._invoke_tool("generate_script", {"x": "unsafe"})
    assert tool.calls == []
    loop.collector["loaded_skills"].append("novel-serial")
    assert loop._available_tools() == [tool]
    assert loop._invoke_tool("generate_script", {"x": "ok"}) == "echo:ok"


def test_skill_gate_fails_closed_without_collector():
    tool = FakeTool()
    tool.name = "submit_videos"
    tool.metadata = {"required_skill": "production-delivery"}
    loop = _loop(FakeModel([]), [tool])
    assert "production-delivery" in loop._invoke_tool("submit_videos", {})
    assert tool.calls == []


def test_long_novel_adds_a_conditional_skill_requirement():
    tool = FakeTool()
    tool.name = "generate_script"
    tool.metadata = {
        "required_skill": "script-generation",
        "conditional_skill": "novel-serial",
    }
    loop = _loop(FakeModel([]), [tool])
    loop.collector = {"loaded_skills": ["script-generation"], "long_novel": False}
    assert loop._available_tools() == [tool]
    loop.collector["long_novel"] = True
    assert loop._available_tools() == []
    assert "novel-serial" in loop._invoke_tool("generate_script", {})
    loop.collector["loaded_skills"].append("novel-serial")
    assert loop._available_tools() == [tool]


def test_tool_timeout_is_recorded_in_run_trajectory():
    model = FakeModel([
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {}, "id": "timeout-1"}]),
        AIMessage(content="工具超时，我先停在这里。"),
    ], stream=False)
    result = _loop(model, [TimeoutTool()]).run([user_message("检查超时")])
    outcome = next(event for event in result.events if event["type"] == "tool_result")
    assert outcome["failed"] is True
    assert outcome["timed_out"] is True
    assert outcome["duration_ms"] >= 0
    assert outcome["run_id"]


def test_missing_model_returns_friendly_notice():
    """没配模型时给可操作提示，不抛异常（审阅动作仍可用）。"""
    loop = AgentLoop(FakeLLM(None, available=False), [])
    result = loop.run([user_message("hi")])
    assert "没有配置对话模型" in result.text
    assert "审阅动作" in result.text
    assert "设置 → 模型" in result.text
    assert ".env" not in result.text


def test_has_images_uses_multimodal_model():
    llm = FakeLLM(FakeModel([AIMessage(content="看图了")], stream=False))
    AgentLoop(llm, [], has_images=True).run([user_message("hi")])
    assert llm.multimodal_calls == 1


def test_no_images_uses_plain_model():
    llm = FakeLLM(FakeModel([AIMessage(content="ok")], stream=False))
    AgentLoop(llm, [], has_images=False).run([user_message("hi")])
    assert llm.multimodal_calls == 0


# ---------- 消息协议边界 ----------


def test_tool_message_dict_survives_round_trip():
    """dict 协议 → LangChain 消息的适配必须保真（尤其 tool_call_id）。

    换 provider 时这个适配层是唯一需要重写的地方，所以要把契约定死。
    """
    from app.chat.loop import _to_lc_messages

    lc = _to_lc_messages([
        system_message("s"),
        user_message([{"type": "text", "text": "看图"}]),
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "echo", "args": {"x": 1}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "结果"},
    ])
    assert lc[0].content == "s"
    assert lc[1].content == [{"type": "text", "text": "看图"}]
    call = lc[2].tool_calls[0]
    assert (call["name"], call["args"], call["id"]) == ("echo", {"x": 1}, "c1")
    assert isinstance(lc[3], ToolMessage)
    assert lc[3].tool_call_id == "c1"


def test_unknown_role_is_skipped_not_fatal():
    from app.chat.loop import _to_lc_messages

    lc = _to_lc_messages([{"role": "weird", "content": "x"}, system_message("s")])
    assert len(lc) == 1


def test_text_extraction_handles_content_blocks():
    from app.chat.loop import _msg_text

    assert _msg_text("abc") == "abc"
    assert _msg_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
    assert _msg_text(None) == ""
    assert _msg_text(123) == "123"


@pytest.mark.parametrize("steps", [1, 2, 8])
def test_max_steps_respected(steps):
    script = [
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {"x": i}, "id": f"c{i}"}])
        for i in range(10)
    ]
    result = _loop(FakeModel(script, stream=False), max_steps=steps).run([user_message("hi")])
    assert result.truncated is True
    assert len(result.events) == steps * 2
