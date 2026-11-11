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
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from ..llm import LLM

log = logging.getLogger(__name__)

#: 一轮对话里最多跑几轮工具。防止模型在工具之间来回打转。
MAX_TOOL_STEPS = 8

#: 消息里的文本截断长度（事件上报用）
_EVENT_TEXT_LIMIT = 160

#: 轮内上下文的字符预算。按字符估算而不是 token（2026-11-11）：全仓不引
#: tiktoken——它也不覆盖 deepseek / 智谱 / MiniMax 的词表，算出来的数只会
#: 给人一种虚假的精确；中文场景下字符数是够用且保守的近似（约 1 字 ≈ 1 token）。
#: 超预算时从最早的历史开始降级丢弃（保 system / 摘要 / 本轮输入 / 最近轨迹）。
_TURN_CHAR_BUDGET = 120_000

#: 回灌给模型的工具结果上限。读类工具自带限额（read_source 4000 字等），
#: 这条兜底防的是**没有自觉限额的工具**把整份大产物（分镜表 JSON 之类）
#: 原样灌进轮内上下文——每一步都会重发它。
_TOOL_RESULT_LIMIT = 6000

#: 回灌前 tool_calls 参数里超长字符串的截断点。create_project 的 raw_text
#: 是整部小说（可能几万字），执行用的是完整 args，但留在 messages 里给
#: 模型「看参数」的副本只需要证明它做过这件事——整本小说每步重发一遍
#: 是轮内上下文最猛的一个爆炸源（见 loop.py 审计注释）。
_ARG_TEXT_LIMIT = 200

#: 单轮对话的 wall-clock 预算（秒）：8 步工具 × 每步一次模型调用 + 可能的
#: 编造拦截重跑，没有整轮预算的话一个请求可以挂很久（模型 SDK 默认超时很长）。
_TURN_WALL_CLOCK = 300.0


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
        # 工具运行期写入的收集器（可选）：本轮中途建了作品时，兜底值跟着走。
        self.collector: dict[str, Any] | None = None
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
        # 整轮时间预算：超限按「工具轮数超限」同款方式优雅收尾（保正文、不再开新步）。
        deadline = time.monotonic() + _TURN_WALL_CLOCK
        # 基线边界：进来时已有 system/摘要/历史/本轮输入；预算兜底只动这个边界之前。
        keep_from = len(messages)

        for _step in range(self.max_steps + 1):
            # 最后一轮不再执行工具：把预算收在回答上，而不是又开一轮工具。
            if _step >= self.max_steps:
                text = "这轮工具调用有点多，先停在这里。你可以继续说一句，我接着处理。"
                yield LoopEvent("_truncated", {})
                yield LoopEvent("_complete", {"text": text})
                yield LoopEvent("token", {"delta": text})
                return
            if time.monotonic() > deadline:
                text = "这一轮跑的时间有点长，先停在这里。你可以继续说一句，我接着处理。"
                yield LoopEvent("_truncated", {})
                yield LoopEvent("_complete", {"text": text})
                yield LoopEvent("token", {"delta": text})
                return

            keep_from = _fit_turn_budget(messages, keep_from)
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
                    "args": short(json.dumps(args, ensure_ascii=False), 200),
                })
                result = self._invoke_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": _clamp(result, _TOOL_RESULT_LIMIT, keep_newlines=True),
                })
                yield LoopEvent("tool_result", {
                    "name": name,
                    "summary": short(result, _EVENT_TEXT_LIMIT),
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
                    # 参数副本做超长截断：create_project(raw_text=整部小说) 的 args
                    # 会随每一步 _to_lc_messages 原样重发——执行用的是调用循环里的
                    # 完整 args（_invoke_tool 先于这条 append 之外拿到 calls），
                    # 这里只是留在上下文里给模型「回看自己说过什么」的存根。
                    "args": _clamp_args(c.get("args") if isinstance(c.get("args"), dict) else {}),
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
        # 这里是唯一的收口处：所有声明了 project_id 的作品级工具统一补上当前作品。
        # 当前作品**动态读 collector**：本轮 create_project 之后，
        # 同一轮里的后续工具（generate_script 等）漏传 project_id 也能兜到新作品上。
        # 此前只补「键存在但为空」，模型整个漏键时必传参数的工具仍在 pydantic
        # 校验层报缺参（注释里宣称的「唯一收口处」言过其实，2026-11-11 补全）。
        pid = self.current_project_id or (self.collector or {}).get("project_id") or ""
        if pid and "project_id" in (getattr(tool, "args", None) or {}):
            args = dict(args or {})
            if not str(args.get("project_id") or "").strip():
                args["project_id"] = pid
        try:
            result = tool.invoke(args)
        except Exception as e:  # noqa: BLE001
            log.warning("工具 %s 执行失败：%s", name, e)
            return f"工具执行失败：{type(e).__name__}: {e}"
        if isinstance(result, str):
            return result
        return str(result)


# ---------------------------------------------------------------------
# 截断小件（内核专用；tools/context.short 从这里再导出，一份实现）
# ---------------------------------------------------------------------


def _msg_chars(m: dict[str, Any]) -> int:
    """一条消息的字符规模估算：文本长度；内容块里的图片按 2000 字当量粗估。"""
    content = m.get("content")
    total = 0
    if isinstance(content, str):
        total += len(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += len(str(block.get("text") or ""))
                elif block.get("type") == "image_url":
                    total += 2000
    for c in m.get("tool_calls") or []:
        if isinstance(c, dict):
            total += sum(len(str(v)) for v in (c.get("args") or {}).values())
    return total


def _clamp(text: str, limit: int, *, keep_newlines: bool = False) -> str:
    """截断长文本并如实标注（诚实的「已截断」比静默截断好——同 refs 的原则）。"""
    if len(text) <= limit:
        return text
    tail = "\n（结果过长已截断；需要细节请用对应读取工具分段查看）" if keep_newlines \
        else " …（参数过长已截断，全文以工具返回为准）"
    return text[:limit] + tail


def _clamp_args(args: dict[str, Any]) -> dict[str, Any]:
    """tool_calls 参数副本：超长字符串（整本小说）截成存根，短值原样。"""
    return {
        k: (v if not isinstance(v, str) or len(v) <= _ARG_TEXT_LIMIT else _clamp(v, _ARG_TEXT_LIMIT))
        for k, v in args.items()
    }


def _fit_turn_budget(messages: list[dict[str, Any]], keep_from: int) -> int:
    """轮内字符预算兜底：超预算时丢**基线历史**里最早的消息。返回新的 keep_from。

    keep_from = 本轮开始时的消息条数（基线边界）：只动边界之前的历史，
    本轮轨迹（assistant + tool 成对）绝不碰——拆开会让 OpenAI 协议报错。
    system 消息（含 [此前对话摘要]）也不动。正常轮内消息到不了这个量
    （工具结果有 _TOOL_RESULT_LIMIT、参数有 _ARG_TEXT_LIMIT），触发即说明
    出现了没自觉限额的大产物——丢最旧历史保当前轮可用。
    """
    guard = 0
    while sum(_msg_chars(m) for m in messages) > _TURN_CHAR_BUDGET and guard < 200:
        guard += 1
        dropped = False
        for i, m in enumerate(messages[:keep_from]):
            if m.get("role") not in ("user", "assistant"):
                continue
            # 本轮输入（基线里最后一条 user 消息）不能丢——丢了模型就不知道用户说了什么。
            if i == keep_from - 1 and m.get("role") == "user":
                break
            del messages[i]
            keep_from -= 1
            dropped = True
            break
        if not dropped:
            break
    return keep_from


def short(text: Any, limit: int) -> str:
    """压成一行短文本并截断（工具轨迹、事件展示共用）。

    唯一实现（2026-11-11 去重）：此前 loop._brief 与 tools/context.short
    是两份逐字相同的函数。context 层从这里再导出，保持工具侧 import 不破。
    """
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


__all__ = [
    "AgentLoop",
    "LoopEvent",
    "LoopResult",
    "MAX_TOOL_STEPS",
    "assistant_message",
    "short",
    "system_message",
    "user_message",
]
