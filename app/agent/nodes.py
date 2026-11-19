# =====================================================================
# nodes.py —— LangGraph 图节点实现
#
# 每个节点都是 `(state) -> partial_update` 的函数。它们由 nodes_factory
# 以闭包形式创建，闭包捕获本次运行所需的：store、llm、基础剧本、项目、
# 原始文本、工具与检索器。这样状态里只放可序列化数据，依赖通过闭包注入。
#
# 节点编排（见 graph.py 的连接）：
#   context -> plan(ReAct+tools) -> propose -> guard(自纠错) -> review(interrupt)
#        -> apply -> finalize            （接受 / 编辑后接受）
#        -> propose                       （重新生成，带反馈回到提议）
#        -> finalize                      （拒绝）
#
# 这是整个项目的「人机协同」核心：Agent 只产出 patch 提议，先经 guard 自纠错，
# 再交给用户做最终决定；接受后走 apply 生成新版本。
# =====================================================================

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ..config import Settings
from ..domain import Script
from ..llm import LLM
from ..pipeline.patch import (
    PatchProposal,
    apply_patch,
    build_patch,
    default_plan,
    fallback_patch,
    validate_script,
)
from ..pipeline.profiles import profile_for, profile_prompt
from ..pipeline.review import DIMENSION_LABELS, review_patch
from .state import (
    STATUS_APPLIED,
    STATUS_CONTEXTING,
    STATUS_FAILED,
    STATUS_PLANNING,
    STATUS_REJECTED,
    STATUS_REVIEWING,
    AgentState,
)
from ..store import Project, Store

# guard 自纠错循环的最大迭代次数，防止「发现问题 -> 重做」死循环。
MAX_PROPOSE_ITERATIONS = 3


def _parse_ops(patch_data: list[dict[str, Any]]) -> list[Any]:
    """将状态中的 patch 字典列表解析为 PatchOp 列表（单次解析，多处复用）。"""
    from ..pipeline.patch import PatchOp

    return [PatchOp.model_validate(op) for op in (patch_data or [])]


def _selected_scenes(script: Script, scene_ids: list[str]) -> list[object]:
    """按 scene_ids 挑选场景；为空时选全部。"""
    if not scene_ids:
        return list(script.scenes)
    wanted = set(scene_ids)
    return [s for s in script.scenes if s.id in wanted]


def _build_context(
    script: Script,
    project: Project,
    raw_text: str,
    *,
    scene_ids: list[str],
    instruction: str = "",
) -> dict[str, Any]:
    """把本次改编所需的上下文组织成一个字典，供提示词与工具使用。

    原文搜索由 Agent 工具按需调用，不在上下文里预塞。
    """
    from ..pipeline.knowledge import format_author_style

    profile = profile_for(project.adaptation_type)
    scenes = _selected_scenes(script, scene_ids)
    scene_list = [
        {
            "id": s.id,
            "title": s.title,
            "purpose": s.purpose,
            "conflict": s.conflict,
            "characters": s.characters,
            "location_id": s.location_id,
            "time": s.time,
            "beats": [b.model_dump(exclude_none=True) for b in s.beats],
        }
        for s in scenes
    ]

    # 作者风格（规则提取）
    author_style = format_author_style(raw_text)

    return {
        "script": {
            "title": script.title,
            "logline": script.logline,
            "themes": script.themes,
            "language": script.language,
            "adaptation": profile,
        },
        "characters": {c.id: {"name": c.name, "role": c.role, "goal": c.goal,
                              "description": c.description} for c in script.characters},
        "locations": {loc.id: {"name": loc.name, "description": loc.description} for loc in script.locations},
        "selected_scenes": scene_list,
        "author_style": author_style,
        "raw_text_excerpt": " ".join((raw_text or "").split())[:2000],
    }


def _system_prompt(settings: Settings, script: Script) -> str:
    """Agent 系统提示词：身份、规则、改编约束、输出格式示例。"""
    profile = profile_for(script.adaptation.type if script.adaptation else "other")
    return (
        "你是剧本改编助手。你只读取上下文并修改「选中的场景」，不要新增场景、人物或地点。\n"
        "已有节拍必须保留原 id；新增节拍可以省略 id 或使用未占用的 beat_数字。\n"
        "对白说话人必须是该场景已有的人物 id。\n"
        "上下文中的 author_style 带有作者语言风格，"
        "改写时请自然借鉴，保持原味，但不要照抄。\n"
        "你可以通过 search_source 工具搜索原文。\n"
        "请严格按以下 JSON 结构输出，字段名不要改动：\n"
        '{\n'
        '  "plan": ["计划步骤一", "计划步骤二"],\n'
        '  "changes": [\n'
        '    {\n'
        '      "scene_id": "scene_001",\n'
        '      "title": "新场景标题（可选）",\n'
        '      "beats": [\n'
        '        {"id": "beat_001", "type": "action", "text": "动作描述"},\n'
        '        {"id": "beat_002", "type": "dialogue", "speaker": "char_xxx", "line": "台词", "emotion": "情绪", "subtext": "潜台词"}\n'
        '      ],\n'
        '      "adaptation_reason": "为什么这么改"\n'
        '    }\n'
        '  ]\n'
        '}\n'
        "注意：plan 必须是字符串数组；改动节拍用 beats 数组，不要用 updates；"
        "只有确实要改的字段才返回。\n"
        f"改编类型 profile：\n{profile_prompt(profile, language=script.language)}\n"
        f"输出语言：{settings.output_language}\n"
        "不要输出整份覆盖文档，只输出真正需要更新的字段。"
    )


def build_nodes(
    store: Store,
    llm: LLM,
    script: Script,
    project: Project,
    raw_text: str,
    *,
    settings: Settings,
    tools: list[Any],
) -> dict[str, Callable[[AgentState], dict[str, Any]]]:
    """构造全部图节点。"""

    def context_node(state: AgentState) -> dict[str, Any]:
        ctx = _build_context(
            script,
            project,
            raw_text,
            scene_ids=state.get("scene_ids", []),
            instruction=state.get("instruction", ""),
        )
        system = SystemMessage(content=_system_prompt(settings, script))
        human = HumanMessage(content=f"用户改编需求：{state.get('instruction','')}")
        return {
            "context": ctx,
            "model_available": llm.available,
            "messages": [system, human],
            "status": STATUS_CONTEXTING,
            "steps": 0,
        }

    def plan_node(state: AgentState) -> dict[str, Any]:
        # 没有可用模型时，跳过 ReAct 工具循环，直接给兜底计划。
        if not llm.available:
            return {"plan": default_plan(), "status": STATUS_PLANNING}
        resp = llm.chat().bind_tools(tools).invoke(state["messages"])
        return {"messages": [resp], "status": STATUS_PLANNING}

    def propose_node(state: AgentState) -> dict[str, Any]:
        instruction = state.get("instruction", "")
        scene_ids = state.get("scene_ids", [])
        critique = state.get("critique") or []
        # 来自「重新生成」的反馈：先并入指令，再重新提议。
        feedback = (state.get("decision") or {}).get("feedback") or ""
        if feedback:
            instruction = f"{instruction}\n（用户补充要求：{feedback}）"
        if llm.available:
            ctx = state.get("context") or {}
            plan = state.get("plan") or []
            guidance = ""
            if critique:
                guidance = "\n自我审阅发现以下问题，请修正后再输出：\n" + "\n".join(f"- {c}" for c in critique)
            prompt_json = json.dumps(ctx, ensure_ascii=False, indent=2)
            prompt = (
                f"请针对选中场景给出结构化改编提议。用户需求：{instruction}\n"
                f"推理计划（仅供参考）：{'；'.join(plan)}\n"
                f"当前上下文：\n{prompt_json}{guidance}"
            )
            proposal = llm.structured(PatchProposal).invoke(
                [SystemMessage(content=_system_prompt(settings, script)), HumanMessage(content=prompt)]
            )
            proposal_dict = proposal.model_dump(exclude_none=True)
            plan, ops = build_patch(proposal, script, selected_scene_ids=scene_ids, instruction=instruction)
            # 重新提议后清空上一条 critique，避免无限引用旧问题。
            critique = []
        else:
            proposal_dict = None
            plan, ops = fallback_patch(script, instruction, scene_ids)
            critique = []
        return {
            "proposal": proposal_dict,
            "plan": plan,
            "patch": [op.model_dump(exclude_none=True) for op in ops],
            "critique": critique,
            "iterations": state.get("iterations", 0),
            "status": STATUS_PLANNING,
        }

    def guard_node(state: AgentState) -> dict[str, Any]:
        """自我审阅：先把当前 patch dry-run 应用，检查是否会破坏结构。

        两条防线：
          1. 硬约束（确定性）：validate_script —— 人物 / 地点引用、id 唯一性、空字段，
             无模型也能跑；发现问题且未超限时回 propose 重做。
          2. 软质量 + 语义一致性（LLM 审阅）：review_patch 对应用后的剧本做多维度打分
             （忠实度 / 一致性 / 冲突 / 风格 / 结构）并列出 error 级一致性问题；
             总分低于阈值或有 error 级问题时回 propose 重做。
        任一防线发现问题即写回 critique 并回到 propose；都通过则交给人类审阅。
        没有模型时走纯规则校验（兜底 patch 通常一次通过）。
        """
        ops = _parse_ops(state.get("patch"))
        try:
            applied = apply_patch(script, ops)
        except Exception as e:  # noqa: BLE001
            return {
                "critique": [f"应用失败：{e}"],
                "iterations": state.get("iterations", 0) + 1,
                "status": STATUS_PLANNING,
            }
        issues = [i for i in validate_script(applied) if i.severity == "error"]
        critique = [f"{i.path}：{i.message}" for i in issues]
        review_dict: dict[str, Any] | None = None
        if llm.available and settings.enable_review_scoring:
            review = review_patch(
                llm,
                base=script,
                applied=applied,
                instruction=state.get("instruction", ""),
                context=state.get("context"),
                threshold=settings.review_score_threshold,
                language=settings.output_language or script.language,
            )
            if review is not None:
                review_dict = review.model_dump(exclude_none=True)
                for iss in review.issues:
                    if iss.severity == "error":
                        label = DIMENSION_LABELS.get(iss.category, iss.category)
                        msg = f"[{label}] {iss.message}"
                        if msg not in critique:
                            critique.append(msg)
        iteration = state.get("iterations", 0)
        if critique and iteration < MAX_PROPOSE_ITERATIONS:
            return {
                "critique": critique,
                "iterations": iteration + 1,
                "status": STATUS_PLANNING,
                "review": review_dict,
            }
        return {
            "critique": critique,
            "iterations": iteration,
            "status": STATUS_REVIEWING,
            "review": review_dict,
        }

    def review_node(state: AgentState) -> dict[str, Any]:
        # 中断：把提议（含上下文与可选动作）呈现给人类；
        # 用户决定后通过 Command(resume=HumanDecision) 恢复。
        from langgraph.types import interrupt

        decision = interrupt(
            {
                "patch": state.get("patch") or [],
                "plan": state.get("plan") or [],
                "instruction": state.get("instruction", ""),
                "proposal": state.get("proposal"),
                "context": state.get("context"),
                "review": state.get("review"),  # 评审打分 + 一致性保障结果
                "choices": ["accept", "edit", "regenerate", "reject"],
            }
        )
        return {"decision": decision}

    def select_ops(state: AgentState) -> list[Any]:
        """按用户选择的下标挑选 patch 操作。

        - 未选择（None）-> 全接受；
        - 显式选择但全部下标无效 -> 返回空（用户无法表达「一条都不要」时，
          把无效选择回退为全接受是危险的：等于悄悄接受了用户没勾的改动）。
        """
        patch = _parse_ops(state.get("patch"))
        decision = state.get("decision") or {}
        indexes = decision.get("patch_indexes")
        if indexes is None:
            return patch
        return [patch[i] for i in indexes if 0 <= i < len(patch)]

    def apply_node(state: AgentState) -> dict[str, Any]:
        decision = state.get("decision") or {}
        # 若人类在中断处「编辑」了 patch，优先采用人工修订后的操作。
        if decision.get("patch"):
            ops = _parse_ops(decision["patch"])
        else:
            ops = select_ops(state)
        if not ops:
            return {
                "status": STATUS_FAILED,
                "new_version_id": None,
                "validation_issues": [],
                "error": "没有可应用的操作（选择为空或下标无效）",
            }
        try:
            new_script = apply_patch(script, ops)
        except Exception as e:  # noqa: BLE001
            # 应用失败不再让整张图崩掉：如实上报，不落版。
            return {
                "status": STATUS_FAILED,
                "new_version_id": None,
                "validation_issues": [],
                "error": f"应用改动失败：{e}",
            }
        issues = validate_script(new_script)
        error_issues = [i for i in issues if i.severity == "error"]
        if error_issues:
            # 与手动编辑路径（api.apply_edit）保持一致：error 必须清零才可落版。
            # 用户勾选的子集 / 人工修订的 patch 未经 guard 预检，这里做最后一道闸。
            return {
                "status": STATUS_FAILED,
                "new_version_id": None,
                "validation_issues": [i.model_dump() for i in issues],
                "error": "; ".join(i.message for i in error_issues),
            }
        version = store.create_version(
            project,
            new_script,
            source_type="agent_adaptation",
            label="AI 改编",
            notes=f"用户需求：{state.get('instruction','')}（应用 {len(ops)} 项）",
            parent_version_id=project.current_version_id,
            set_current=True,
        )
        return {
            "status": STATUS_APPLIED,
            "new_version_id": version.id,
            "validation_issues": [i.model_dump() for i in issues],
            "error": None,
        }

    def finalize_node(state: AgentState) -> dict[str, Any]:
        decision = state.get("decision") or {}
        action = decision.get("action", "reject")
        new_version_id = state.get("new_version_id")
        if new_version_id:
            # accept / edit 都会真正落版，视为 applied；其余（reject/regenerate 兜底）为 rejected。
            status = STATUS_APPLIED if action in ("accept", "edit") else STATUS_REJECTED
        elif action in ("accept", "edit"):
            # 应用 / 校验失败，未生成版本：如实记为 failed，不冒充 applied。
            status = STATUS_FAILED
        else:
            status = STATUS_REJECTED
        fields: dict[str, Any] = {
            "status": status,
            "decision": decision,
            "result_version_id": new_version_id,
        }
        if status == STATUS_FAILED:
            fields["error_message"] = state.get("error")
        store.update_agent_run(state["run_id"], **fields)
        return {"status": status}

    return {
        "context": context_node,
        "plan": plan_node,
        "propose": propose_node,
        "guard": guard_node,
        "review": review_node,
        "apply": apply_node,
        "finalize": finalize_node,
    }
