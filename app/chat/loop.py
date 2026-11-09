# =====================================================================
# loop.py —— 对话 Agent 内核：一个显式的工具循环
#
# 为什么不再用 LangGraph 的 StateGraph + ToolNode：
#   - 状态通道（reducer）行为不直观——踩过 has_images 这类键在节点间丢失的坑；
#   - 循环是「LLM → 工具 → LLM」，本来就该写成 while，画成图反而要多读一层；
#   - 依赖重、调试是黑盒：出问题时看不到「第几轮、调了什么、回了什么」。
#
# 现在就是一段可以单步调试的循环：
#
#   for step in range(max_steps):
#       resp = model(messages, tools)
#       if 没有 tool_calls: break
#       for call: messages.append(tool 结果)
#
# 消息用 **OpenAI 兼容的 dict 协议**（role/content/tool_calls/tool_call_id）——
# 我们所有 provider（DeepSeek / 智谱 / OpenAI / Qwen）都是这个协议，
# 所以协议本身就是通用格式，不绑任何框架类型。
# LangChain 只在「真正调模型的那一刻」被适配一次（`to_lc_messages`），
# 将来换成自写 HTTP provider 时，只需要替换 `_transport`。
# =====================================================================

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterator

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from ..llm import LLM

log = logging.getLogger(__name__)

#: 一轮对话里最多跑几轮工具。防止模型在工具之间来回打转。
MAX_TOOL_STEPS = 8

#: 消息里的文本截断长度（事件上报用）
_EVENT_TEXT_LIMIT = 160


# ---------------------------------------------------------------------
# 消息协议（OpenAI 兼容 dict）
# ---------------------------------------------------------------------


def system_message(text: str) -> dict[str, Any]:
    return {"role": "system", "content": text}


def user_message(content: Any) -> dict[str, Any]:
    """用户消息。content 可以是字符串，也可以是 [{type:text|image_url,...}] 内容块。"""
    return {"role": "user", "content": content}


def assistant_message(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _to_lc_messages(messages: list[dict[str, Any]]) -> list[Any]:
    """把我们的 dict 协议适配成 LangChain 消息（仅用于调用模型）。

    这是唯一的框架边界：将来换自写 HTTP provider，删掉这个函数即可。
    """
    out: list[Any] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            out.append(SystemMessage(content=content or ""))
        elif role == "user":
            out.append(HumanMessage(content=content or ""))
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            lc_calls = [
                {"name": c.get("name", ""), "args": c.get("args") or {}, "id": c.get("id") or ""}
                for c in calls
                if isinstance(c, dict)
            ]
            out.append(AIMessage(content=content or "", tool_calls=lc_calls))
        elif role == "tool":
            out.append(ToolMessage(
                content=content if isinstance(content, str) else str(content or ""),
                tool_call_id=m.get("tool_call_id") or "",
                name=m.get("name") or "",
            ))
        # 未知 role 直接忽略：宁可少一条，不要整轮崩掉。
    return out


def _msg_text(content: Any) -> str:
    """把模型 content（str / 内容块列表）规整成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def _reasoning_delta(chunk: Any) -> str:
    """从流式块里取思考增量（DeepSeek / GLM 的 reasoning_content）。"""
    kw = getattr(chunk, "additional_kwargs", None) or {}
    value = kw.get("reasoning_content") or getattr(chunk, "reasoning_content", None)
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------


@dataclass
class LoopEvent:
    """循环产出的事件（上层直接映射成 SSE）。"""

    type: str  # thinking / token / tool_call / tool_result
    data: dict[str, Any]


@dataclass
class LoopResult:
    """一轮对话的最终结果。"""

    text: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: 是否因为工具轮数超限而提前收尾
    truncated: bool = False


# ---------------------------------------------------------------------
# 内核
# ---------------------------------------------------------------------


class AgentLoop:
    """显式的工具调用循环。

    ```python
    loop = AgentLoop(llm, tools, has_images=bool(images))
    for ev in loop.stream(messages):
        if ev.type == "token":
            yield ev.data["delta"]
    ```
    """

    def __init__(
        self,
        llm: LLM,
        tools: list[Any],
        *,
        max_steps: int = MAX_TOOL_STEPS,
        has_images: bool = False,
        current_project_id: str | None = None,
    ) -> None:
        self.llm = llm
        self.tools = list(tools)
        self.max_steps = max_steps
        self.has_images = has_images
        # 本轮对话所属的作品：模型漏传/传空 project_id 时用它兜底。
        self.current_project_id = current_project_id or ""
        self._by_name = {getattr(t, "name", ""): t for t in self.tools if getattr(t, "name", "")}

    # ---- 对外 ----

    def stream(self, messages: list[dict[str, Any]]) -> Iterator[LoopEvent]:
        """跑一轮对话，边跑边吐事件。messages 会被就地追加（调用方可用最终列表）。"""
        for event in self._run(messages, stream=True):
            yield event

    def run(self, messages: list[dict[str, Any]]) -> LoopResult:
        """跑一轮对话，收齐结果（非流式路径用）。"""
        text_parts: list[str] = []
        events: list[dict[str, Any]] = []
        last_complete = ""
        truncated = False
        for ev in self._run(messages, stream=False):
            if ev.type == "token":
                text_parts.append(str(ev.data.get("delta") or ""))
            elif ev.type == "_complete":
                last_complete = str(ev.data.get("text") or "")
            elif ev.type == "_truncated":
                truncated = True
            else:
                events.append({"type": ev.type, **ev.data})
        # 与流式路径保持一致：优先用累积正文，没有才退回最近的完整消息。
        text = "".join(text_parts) or last_complete
        return LoopResult(text=text, events=events, messages=messages, truncated=truncated)

    # ---- 内部 ----

    def _model(self) -> Any:
        if not self.llm.available:
            return None
        return self.llm.chat_multimodal() if self.has_images else self.llm.chat()

    def _run(self, messages: list[dict[str, Any]], *, stream: bool) -> Iterator[LoopEvent]:
        model = self._model()
        if model is None:
            text = (
                "当前没有配置对话模型（在 .env 设置 OPENAI_API_KEY 或 DEEPSEEK_API_KEY 后重启）。"
                "不过审阅动作（接受 / 拒绝 / 重新生成）仍然可用；"
                "也可以先用 REST / CLI 方式新建项目并生成初稿。"
            )
            yield LoopEvent("_complete", {"text": text})
            yield LoopEvent("token", {"delta": text})
            return

        bound = model.bind_tools(self.tools) if self.tools else model

        for _step in range(self.max_steps + 1):
            # 最后一轮不再执行工具：把预算收在回答上，而不是又开一轮工具。
            if _step >= self.max_steps:
                text = "这轮工具调用有点多，先停在这里。你可以继续说一句，我接着处理。"
                yield LoopEvent("_truncated", {})
                yield LoopEvent("_complete", {"text": text})
                yield LoopEvent("token", {"delta": text})
                return

            resp = yield from self._call(bound, messages, stream=stream)
            text = _msg_text(getattr(resp, "content", None))
            calls = list(getattr(resp, "tool_calls", None) or [])

            messages.append(self._assistant_dict(text, calls))
            if not calls:
                yield LoopEvent("_complete", {"text": text})
                return

            for call in calls:
                name = str(call.get("name") or "")
                args = call.get("args") if isinstance(call.get("args"), dict) else {}
                call_id = str(call.get("id") or "")
                yield LoopEvent("tool_call", {
                    "name": name,
                    "args": _brief(json.dumps(args, ensure_ascii=False), 200),
                })
                result = self._invoke_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result,
                })
                yield LoopEvent("tool_result", {
                    "name": name,
                    "summary": _brief(result, _EVENT_TEXT_LIMIT),
                })

    def _call(self, bound: Any, messages: list[dict[str, Any]], *, stream: bool):
        """调一次模型。流式时逐块吐事件，最后返回聚合后的完整消息。"""
        lc = _to_lc_messages(messages)
        if not stream:
            return bound.invoke(lc)

        acc: Any = None
        for chunk in bound.stream(lc):
            if acc is None:
                acc = chunk
            else:
                acc = acc + chunk
            reasoning = _reasoning_delta(chunk)
            if reasoning:
                yield LoopEvent("thinking", {"delta": reasoning})
            delta = _msg_text(getattr(chunk, "content", None))
            if delta:
                yield LoopEvent("token", {"delta": delta})

        # 流结束：工具调用的完整形态可能只在聚合块里（分片合并出来的）。
        return acc if acc is not None else AIMessage(content="")

    @staticmethod
    def _assistant_dict(text: str, calls: list[Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"role": "assistant", "content": text or ""}
        if calls:
            out["tool_calls"] = [
                {
                    "id": str(c.get("id") or ""),
                    "name": str(c.get("name") or ""),
                    "args": c.get("args") if isinstance(c.get("args"), dict) else {},
                }
                for c in calls
            ]
        return out

    def _invoke_tool(self, name: str, args: dict[str, Any]) -> str:
        """执行一个工具。异常不抛出——包成文本回给模型，让它自己决定怎么补救。"""
        tool = self._by_name.get(name)
        if tool is None:
            return f"未知工具：{name}。可用工具：{', '.join(sorted(self._by_name)) or '无'}"
        # 模型常把 project_id 漏掉或传空（实测：get_status("") 会答「还没有剧本版本」）。
        # 这里是唯一的收口处：所有作品级工具统一补上当前作品。
        if self.current_project_id:
            args = dict(args or {})
            if "project_id" in args and not str(args.get("project_id") or "").strip():
                args["project_id"] = self.current_project_id
        try:
            result = tool.invoke(args)
        except Exception as e:  # noqa: BLE001
            log.warning("工具 %s 执行失败：%s", name, e)
            return f"工具执行失败：{type(e).__name__}: {e}"
        if isinstance(result, str):
            return result
        return str(result)


def _brief(text: Any, limit: int) -> str:
    """压成一行短文本（事件里展示用）。"""
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


__all__ = [
    "AgentLoop",
    "LoopEvent",
    "LoopResult",
    "MAX_TOOL_STEPS",
    "assistant_message",
    "system_message",
    "user_message",
]
