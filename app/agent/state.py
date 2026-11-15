# =====================================================================
# state.py —— LangGraph 状态定义
#
# 状态是贯穿整个图、在节点之间流动与更新的数据结构。
# 我们用 TypedDict 声明，并用 LangGraph 的 reducer 处理「消息累积」：
#   - messages 使用 add_messages：每次工具 / 模型返回都会追加，而不是覆盖。
#   - 其余字段默认是「整体覆盖」语义。
#
# 状态既在图中流动，也被 checkpointer 持久化到磁盘 / 内存，
# 因此没有把数据库会话、模型等不可序列化对象放进状态 ——
# 这些通过闭包注入到各节点，保证状态干净、可存档、可恢复。
# =====================================================================

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """剧本改编 Agent 的图状态。

    字段说明：
    - project_id / base_version_id / scene_ids / instruction：本次运行的输入目标。
    - model_available：是否配置了模型，决定是否走 ReAct 工具循环。
    - context：已采集的上下文（剧本概况、人物、地点、原文字段）。
    - messages：ReAct 会话消息（含工具调用与工具结果）。
    - proposal：LLM 原始结构化提议（PatchProposal 的字典形式）。
    - plan：推理计划 / 兜底计划，供前端展示。
    - patch：规范化后的 patch 操作清单（可逐条接受）。
    - decision：用户审阅决定（accept / reject + 接受下标）。
    - status：运行状态（contexting / planning / reviewing / applied / rejected）。
    - error：错误信息。
    """

    project_id: str
    base_version_id: str
    scene_ids: list[str]
    instruction: str
    model_available: bool
    run_id: str

    context: dict[str, Any]
    messages: Annotated[list[AnyMessage], add_messages]

    proposal: dict[str, Any] | None
    plan: list[str]
    patch: list[dict[str, Any]]
    decision: dict[str, Any] | None
    status: str
    error: str | None
    steps: int
    new_version_id: str | None
    validation_issues: list[dict[str, Any]]
    # --- 自我审阅 / 重做循环 ---
    critique: list[str]
    iterations: int
    # --- LLM 审阅结果（评审打分 + 一致性保障，见 app/pipeline/review.py）---
    review: dict[str, Any] | None
    # --- 运行过程中实际执行的节点序列（由 stream 采集，供前端展示进度） ---
    node_path: list[str]


# 各阶段状态常量。
STATUS_CONTEXTING = "contexting"
STATUS_PLANNING = "planning"
STATUS_REVIEWING = "reviewing"
STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"  # 接受后应用/校验失败，未落版
