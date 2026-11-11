# =====================================================================
# memory.py —— 用户级三层记忆 + 会话滚动摘要
#
# 替代 RAG 的结构化记忆：存 DB memories 表，作用域隔离
#   preference（全局偏好）> decision（项目决定）> feedback（对话反馈）
# Agent 通过 conductor 的 remember / recall 工具主动读写；
# learn_from_decision() 从用户 accept / reject / regenerate 行为自动沉淀。
#
# 滚动摘要：长对话不再掐头丢弃——超阈值时把旧消息压缩成一段事实清单
# （存 conversations.summary_json），上下文 = 摘要 + 最近消息。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

from .store import Store

log = logging.getLogger(__name__)

# 上下文里保留的最近消息条数；超过「保留窗口 + 触发余量」才压缩一次。
RECENT_KEEP = 16
COMPRESS_TRIGGER = 10  # 待压缩消息至少这么多条才调一次 LLM，避免频繁摘要拖慢回复

_KIND_LABELS = {"preference": "用户偏好", "decision": "项目决定", "feedback": "对话反馈"}


# ---------- 从审阅决策自动学习 ----------


def learn_from_decision(
    store: Store,
    *,
    project_id: str,
    conversation_id: str | None,
    action: str,
    instruction: str,
    feedback: str | None = None,
    patch_count: int = 0,
) -> None:
    """从用户对改编提议的审阅动作沉淀记忆（确定性文本，不调 LLM）。

    接受 → 项目决定（这轮改了什么，后续对话延续这个方向）；
    拒绝/重新生成带反馈 → 对话反馈（用户不想要什么，重试时避开）。
    失败不抛出：学习是副产品，绝不能挡住审阅主流程。
    """
    try:
        instruction = (instruction or "").strip()
        feedback = (feedback or "").strip()
        if action == "accept":
            content = f"接受了改编「{instruction or '未记录指令'}」"
            if patch_count:
                content += f"（采纳 {patch_count} 处改动）"
            content += "——后续改编保持这个方向。"
            store.add_memory(
                kind="decision", scope="project", project_id=project_id,
                content=content, source="auto",
            )
        elif action in ("reject", "regenerate") and feedback:
            label = "拒绝" if action == "reject" else "要求重新生成"
            store.add_memory(
                kind="feedback", scope="conversation", project_id=project_id,
                conversation_id=conversation_id,
                content=f"{label}了「{instruction or '上一版'}」，反馈：{feedback}。重写时避开这个方向。",
                source="auto",
            )
    except Exception as e:  # noqa: BLE001
        log.warning("从审阅决策学习记忆失败（不阻断审阅）：%s", e)


# ---------- 记忆注入（系统提示） ----------


def format_memory_note(
    store: Store,
    *,
    project_id: str | None,
    conversation_id: str | None,
    max_items: int = 12,
) -> str:
    """把当前可见记忆格式化成系统提示的一个小节；没有记忆返回空串。

    global → project → conversation 顺序（list_memories 已排好），
    后出现的更具体，模型按「具体覆盖一般」理解。
    """
    try:
        rows = store.list_memories(project_id=project_id, conversation_id=conversation_id)
    except Exception as e:  # noqa: BLE001
        log.warning("读取记忆失败（跳过注入）：%s", e)
        return ""
    rows = [m for m in rows if m.content.strip()][:max_items]
    if not rows:
        return ""
    lines = [f"- （{_KIND_LABELS.get(m.kind, m.kind)}）{m.content.strip()}" for m in rows]
    return (
        "\n\n[用户记忆]\n以下是过去积累的用户偏好与项目决定，"
        "改编和交流时遵循它们；与本次指令冲突时以本次指令为准：\n"
        + "\n".join(lines)
    )


# ---------- 会话滚动摘要 ----------


def compress_history_if_needed(
    store: Store,
    llm: Any,
    conversation_id: str,
) -> tuple[str, list[Any]]:
    """读对话历史，必要时把旧消息压缩成滚动摘要。

    返回 (摘要文本 or "", 进入上下文的最近消息列表 ChatMessage)。
    - 消息不多或模型不可用：原样返回最近 RECENT_KEEP 条，摘要为空；
    - 达到阈值：把「已摘要覆盖之外、最近窗口之前」的消息合并进旧摘要，
      存回 conversations.summary_json。LLM 失败则回落为截断，不阻塞对话。

    覆盖进度用 **covered_until_id 锚点**（最后一条已并入摘要的消息 id），
    不再用绝对计数：此前 covered_count 是「从会话开头累计的条数」，而读库
    只取最新 200 条——总条数超 200 后 `covered` 钳到窗口头、pending 永远空，
    摘要从此停摆，第 200 条窗口之外、摘要覆盖之外的中段历史**静默丢失**
    （2026-11-11 审计修掉的 bug；锚点滚出窗口时退化为「窗口内未保留段
    全部重摘」，合并式摘要天然去重，宁多读一遍不丢事实）。
    """
    messages = store.list_chat_messages(conversation_id, limit=200)
    recent_start = max(0, len(messages) - RECENT_KEEP)
    recent = messages[recent_start:]
    if len(messages) <= RECENT_KEEP + COMPRESS_TRIGGER:
        return "", recent
    if not getattr(llm, "available", False):
        return "", recent

    conv = store.get_conversation(conversation_id)
    state = (conv.summary if conv else {}) or {}
    old_summary = str(state.get("summary") or "")
    covered_until_id = str(state.get("covered_until_id") or "")
    if not covered_until_id and state.get("covered_count") is not None:
        # 一次性换算旧格式（covered_count 绝对计数）：能对上窗口内就续用，
        # 对不上（老库窗口外）退回窗口开头——下一轮摘要会把未覆盖段并回去。
        legacy = int(state.get("covered_count") or 0)
        covered_until_id = messages[legacy - 1].id if 0 < legacy <= len(messages) else ""

    anchor_pos = next(
        (i for i, m in enumerate(messages) if m.id == covered_until_id),
        -1,
    )
    pending_start = anchor_pos + 1 if anchor_pos >= 0 else 0

    pending = messages[pending_start:recent_start]
    if len(pending) < COMPRESS_TRIGGER:
        # 没攒够新内容：沿用旧摘要。此前 pending 段（覆盖点之后、最近窗口之前）
        # 既不在摘要也不进上下文——不足触发阈值时它「暂时失忆」。现在把它并进
        # 返回窗口：条数少（<10），成本可忽略，事实不丢。
        return old_summary, messages[pending_start:]

    try:
        new_summary = _summarize(llm, old_summary, pending)
    except Exception as e:  # noqa: BLE001
        # 回落截断：锚点不动，这一段下轮攒够还会上摘（合并式摘要去重），
        # 只是这一轮中段暂缺——比把十几条原文全塞进上下文划算。
        log.warning("会话摘要生成失败（本次回落截断）：%s", e)
        return old_summary, recent

    store.set_conversation_summary(conversation_id, {
        "summary": new_summary,
        "covered_until_id": pending[-1].id,
    })
    return new_summary, recent


_SUMMARY_PROMPT = (
    "你是对话记录员。把「已有摘要」和「新一段对话」合并成一份更新的事实清单，"
    "供后续对话作为唯一记忆使用。要求：\n"
    "1. 只保留事实：项目状态（进行到哪一步）、用户偏好与明确要求、已做的决定"
    "（批准/拒绝了什么）、未决事项；\n"
    "2. 寒暄、过程性讨论、重复内容一律丢弃；\n"
    "3. 用简体中文短句列表，总长不超过 300 字；没有可保留的内容就返回空串。"
)


def _summarize(llm: Any, old_summary: str, pending: list[Any]) -> str:
    transcript = "\n".join(
        f"{'用户' if m.role == 'user' else '助手'}：{(m.content or '').strip()[:400]}"
        for m in pending
        if (m.content or "").strip()
    )
    user_text = ""
    if old_summary:
        user_text += f"[已有摘要]\n{old_summary}\n\n"
    user_text += f"[新对话]\n{transcript}"
    resp = llm.chat().invoke([
        {"role": "system", "content": _SUMMARY_PROMPT},
        {"role": "user", "content": user_text},
    ])
    text = getattr(resp, "content", "") or ""
    if isinstance(text, list):
        text = " ".join(
            b.get("text", "") for b in text if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(text).strip()
