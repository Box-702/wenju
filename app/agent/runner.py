# =====================================================================
# agent.py —— Agent 运行服务（无 RAG 版）
#
# 薄服务层，把 HTTP 请求和 LangGraph 图对接。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import Command

from ..config import Settings
from .graph import build_run_graph, thread_config
from ..llm import LLM
from ..pipeline.patch import PatchOp, apply_patch, validate_script
from ..store import Project, ScriptVersion, Store

log = logging.getLogger(__name__)


def _select_ops(patch: list[dict[str, Any]], indexes: list[int] | None) -> list[PatchOp]:
    """按用户勾选下标挑选 patch 操作。"""
    ops = [PatchOp.model_validate(op) for op in patch]
    if indexes is None:
        return ops
    return [ops[i] for i in indexes if 0 <= i < len(ops)]


def start_agent_run(
    store: Store,
    llm: LLM,
    settings: Settings,
    *,
    project: Project,
    base_version: ScriptVersion,
    instruction: str,
    scene_ids: list[str],
    model_label: str | None = None,
) -> dict[str, Any]:
    """发起一次 Agent 改编运行，并返回「等待审阅」的提议。"""
    run_id = _new_run_id()
    graph = build_run_graph(
        store,
        llm,
        settings,
        project=project,
        base_script=base_version.script,
        raw_text=project.raw_text,
        base_version_id=base_version.id,
    )
    input_state = {
        "project_id": project.id,
        "base_version_id": base_version.id,
        "scene_ids": scene_ids,
        "instruction": instruction,
        "run_id": run_id,
    }
    steps: list[str] = []
    plan: list[str] = []
    patch: list[dict[str, Any]] = []
    review: dict[str, Any] | None = None
    status = "reviewing"
    error = None
    try:
        interrupt_value, steps = _stream_to_interrupt(graph, input_state, thread_config(run_id))
        payload = interrupt_value or {}
        plan = payload.get("plan") or []
        patch = payload.get("patch") or []
        review = payload.get("review")
    except Exception as e:  # noqa: BLE001
        log.warning("Agent 运行失败，降级为说明性建议：%s", e)
        from ..pipeline.patch import fallback_patch

        fb_plan, fb_ops = fallback_patch(base_version.script, instruction, scene_ids)
        plan = fb_plan + [f"（模型调用失败，已保留说明性建议：{e}）"]
        patch = [op.model_dump(exclude_none=True) for op in fb_ops]
        status = "reviewing"
        error = str(e)

    store.create_agent_run(
        run_id=run_id,
        project_id=project.id,
        base_version_id=base_version.id,
        user_prompt=instruction,
        scene_ids=scene_ids,
        plan=plan,
        patch=patch,
        steps=steps,
        status=status,
        # 记录真实使用的厂商（没显式传时取 LLM 门面解析出的 provider_label），
        # 否则所有运行记录都会写成默认值，事后无法判断是哪家模型跑的。
        model=(model_label or llm.provider_label) if llm.available else "local-rule-fallback",
        error_message=error,
    )
    return {
        "run_id": run_id,
        "plan": plan,
        "patch": patch,
        "steps": steps,
        "status": status,
        "error": error,
        "review": review,
    }


def _stream_to_interrupt(graph: Any, input: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """用 graph.stream 运行到中断，返回 (中断载荷, 节点轨迹)。"""
    interrupt_value: dict[str, Any] | None = None
    steps: list[str] = []
    for chunk in graph.stream(input, config, stream_mode="updates"):
        for node_name, update in chunk.items():
            if node_name == "__interrupt__":
                interrupt_value = update[0].value
            else:
                steps.append(node_name)
    return interrupt_value, steps


def resume_agent_run(
    store: Store,
    llm: LLM,
    settings: Settings,
    *,
    run_id: str,
    action: str,
    patch_indexes: list[int] | None = None,
    patch: list[dict[str, Any]] | None = None,
    feedback: str | None = None,
) -> dict[str, Any]:
    """恢复被中断的图，执行用户决策。"""
    run = store.get_agent_run(run_id)
    if run is None:
        return {"status": "not_found", "error": "运行记录不存在"}
    if run.status in ("applied", "rejected", "stale"):
        return {"status": run.status, "new_version_id": run.result_version_id,
                "error": run.error_message}
    project = store.get_project(run.project_id)
    base_version = store.get_version(run.base_version_id)
    if project is None or base_version is None:
        return {"status": "failed", "error": "找不到项目或基础版本"}
    if action in ("accept", "edit") and project.current_version_id != run.base_version_id:
        message = "这条改编提议基于旧剧本，当前剧本已有新版；请基于最新版重新发起改编"
        store.update_agent_run(run_id, status="stale", error_message=message)
        return {"status": "stale", "error": message}

    decision = {
        "action": action,
        "patch_indexes": patch_indexes,
        "patch": patch,
        "feedback": feedback,
    }
    try:
        graph = build_run_graph(
            store,
            llm,
            settings,
            project=project,
            base_script=base_version.script,
            raw_text=project.raw_text,
            base_version_id=base_version.id,
        )
        interrupt_value, steps = _stream_to_interrupt(
            graph, Command(resume=decision), thread_config(run_id)
        )
        if interrupt_value is not None:
            new_plan = interrupt_value.get("plan") or run.plan
            new_patch = interrupt_value.get("patch") or run.patch
            new_review = interrupt_value.get("review")
            store.update_agent_run(
                run_id,
                status="reviewing",
                plan=new_plan,
                patch=new_patch,
                steps=run.steps + steps,
                decision=decision,
            )
            return {
                "status": "reviewing",
                "plan": new_plan,
                "patch": new_patch,
                "steps": run.steps + steps,
                "decision": decision,
                "review": new_review,
            }
        final_state = graph.get_state(thread_config(run_id)).values
        status = final_state.get("status", "applied")
        store.update_agent_run(run_id, steps=run.steps + steps)

        return {
            "status": status,
            "new_version_id": final_state.get("new_version_id"),
            "decision": decision,
            "steps": run.steps + steps,
        }
    except Exception as e:  # noqa: BLE001
        log.warning("LangGraph 恢复失败，使用 direct-apply 兜底：%s", e)
        return _apply_directly(store, run, project, base_version, decision)


def _apply_directly(
    store: Store,
    run: Any,
    project: Project,
    base_version: ScriptVersion,
    decision: dict[str, Any],
) -> dict[str, Any]:
    """线程丢失时的兜底。"""
    action = decision.get("action", "reject")
    if action not in ("accept", "edit"):
        store.update_agent_run(run.id, status="rejected", decision=decision)
        return {"status": "rejected", "decision": decision, "fallback": True}

    if decision.get("patch"):
        ops = [PatchOp.model_validate(op) for op in decision["patch"]]
    else:
        ops = _select_ops(run.patch, decision.get("patch_indexes"))
    if not ops:
        message = "没有可应用的操作（选择为空或下标无效）"
        store.update_agent_run(run.id, status="failed", decision=decision, error_message=message)
        return {"status": "failed", "error": message, "decision": decision, "fallback": True}
    new_script = apply_patch(base_version.script, ops)
    issues = validate_script(new_script)
    error_issues = [i for i in issues if i.severity == "error"]
    if error_issues:
        message = "; ".join(i.message for i in error_issues)
        store.update_agent_run(run.id, status="failed", decision=decision, error_message=message)
        return {
            "status": "failed",
            "error": message,
            "decision": decision,
            "fallback": True,
            "validation_issues": [i.model_dump() for i in issues],
        }
    version = store.create_version(
        project,
        new_script,
        source_type="agent_adaptation",
        label="AI 改编",
        notes=f"用户需求：{run.user_prompt}（应用 {len(ops)} 项，兜底应用）",
        parent_version_id=base_version.id,
        set_current=True,
    )
    store.update_agent_run(
        run.id,
        status="applied",
        decision=decision,
        result_version_id=version.id,
    )
    return {
        "status": "applied",
        "new_version_id": version.id,
        "decision": decision,
        "fallback": True,
        "validation_issues": [i.model_dump() for i in issues],
    }


def get_run(store: Store, run_id: str) -> dict[str, Any] | None:
    """读取一次运行的当前状态与提议。"""
    run = store.get_agent_run(run_id)
    if run is None:
        return None
    return {
        "run_id": run.id,
        "project_id": run.project_id,
        "base_version_id": run.base_version_id,
        "result_version_id": run.result_version_id,
        "user_prompt": run.user_prompt,
        "scene_ids": run.scene_ids,
        "plan": run.plan,
        "patch": run.patch,
        "steps": run.steps,
        "status": run.status,
        "decision": run.decision,
        "model": run.model,
        "error_message": run.error_message,
        "created_at": run.created_at.isoformat(),
    }


def _new_run_id() -> str:
    from ..store import gen_id
    return gen_id("run")
