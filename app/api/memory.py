# =====================================================================
# memory.py —— 记忆管理 API
#
# 记忆此前只有对话工具能写（remember / forget），用户在界面上**看不到**
# 自己让 Agent 记住了什么——「凭什么这么记我」说不清，错了也没地方改。
# 这里给出列表、启停与删除三件事。
#
# 作用域口径与注入提示词时一致（app/memory.py / Store.list_memories）：
# global 全可见；project / conversation 只对归属上下文可见。
# =====================================================================

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from . import deps

router = APIRouter()


def _memory_dict(m: Any) -> dict[str, Any]:
    return {
        "id": m.id,
        "kind": m.kind,
        "scope": m.scope,
        "content": m.content,
        "source": m.source,
        "active": bool(m.active),
        "project_id": m.project_id,
        "conversation_id": m.conversation_id,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/memories")
def list_memories(
    project_id: str | None = Query(default=None),
    conversation_id: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
) -> dict[str, Any]:
    """列出对当前上下文可见的记忆（按 全局 → 作品 → 本次对话 排序）。"""
    store = deps.store()
    rows = store.list_memories(
        project_id=project_id,
        conversation_id=conversation_id,
        include_inactive=include_inactive,
    )
    return {"memories": [_memory_dict(m) for m in rows]}


@router.put("/memories/{memory_id}")
def set_memory_active(
    memory_id: str,
    active: bool = Query(default=True),
) -> dict[str, Any]:
    """启用 / 停用一条记忆。停用后不再注入提示词，但留着可回查。"""
    store = deps.store()
    m = store.set_memory_active(memory_id, active)
    if m is None:
        raise HTTPException(404, "记忆不存在")
    return _memory_dict(m)


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: str) -> dict[str, Any]:
    """删除一条记忆。"""
    if not deps.store().delete_memory(memory_id):
        raise HTTPException(404, "记忆不存在")
    return {"id": memory_id, "deleted": True}
