# =====================================================================
# production.py —— 小说 → 提示词 的生产工作流（LangGraph 状态图）
#
# 定位：这是**外部对话 Agent 的一个工具**，不是另一层编排。用户说
# 「开始制作 / 一键出提示词」时由对话工具（或 REST）启动，在后台线程里
# 按固定顺序跑完五个阶段，产出「待审提示词」就停下——出不出视频永远
# 由用户决定（批准 → 提交出片是既有门禁，这里不碰）。
#
# 五个阶段（每个节点 = 一个阶段，产物全部落库，幂等可重跑）：
#   script   初始化剧本：没有初稿就跑生成流水线，有就直接复用；
#   director 导演拆解：剧本 → 镜头方案（breakdown + video_version）；
#   style    美术风格指南：视觉锚唯一来源（merge，不冲掉人工选角）；
#   refs     资产库：人物白模图 + 场景图（生成 → 质检 → 注册）；
#   prompts  视频提示词：锚逐字复用，特殊对象带资产库映射编号。
#
# 为什么用 LangGraph 而不是普通函数串行：阶段会继续加（对白配音预演、
# 分集…），状态图让「从某个阶段重跑」「插一个审批关卡」有现成落点；
# 且状态可序列化，与改编图的 interrupt/resume 模式一致。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from ..config import Settings
from ..llm import LLM
from ..store import Store

log = logging.getLogger(__name__)

# 阶段名（状态机与进度上报共用，顺序即执行顺序）。
STAGES = ("script", "director", "style", "refs", "prompts")

STAGE_LABELS = {
    "script": "初始化剧本",
    "director": "导演拆解",
    "style": "视觉风格指南",
    "refs": "资产库（人物白模 + 场景图）",
    "prompts": "视频提示词",
}


class ProductionState(TypedDict, total=False):
    """生产工作流状态：只存 id 与计数，重活都在节点里落库。"""

    project_id: str
    stage: str                     # 当前完成的阶段
    error: str | None
    script_version_id: str | None
    breakdown_count: int
    refs_count: int
    prompt_version_id: str | None
    notes: list[str]               # 各阶段的简报（最终汇总给用户）


def build_production_graph(
    store: Store,
    llm: LLM,
    settings: Settings,
    on_progress: Callable[[str, str], None] | None = None,
):
    """构建生产工作流图。on_progress(stage, message) 供任务面板实时上报。"""

    def _report(stage: str, message: str) -> None:
        if on_progress:
            try:
                on_progress(stage, message)
            except Exception:  # noqa: BLE001
                pass

    def _project(state: ProductionState):
        p = store.get_project(state["project_id"])
        if p is None:
            raise ValueError(f"项目不存在：{state['project_id']}")
        return p

    # ---- 阶段 1：初始化剧本 ----

    def script_node(state: ProductionState) -> dict[str, Any]:
        _report("script", "初始化剧本…")
        p = _project(state)
        version = store.latest_version(p)
        if version is not None:
            _report("script", f"已有剧本版本 {version.id}，直接复用")
            return {
                "stage": "script", "script_version_id": version.id,
                "notes": [f"剧本：复用现有版本 {version.id}"],
            }
        from ..pipeline.generation import generate_script as run_generation

        script, artifacts = run_generation(
            llm, settings,
            title=p.title, raw_text=p.raw_text,
            adaptation_type=p.adaptation_type, language=p.language,
        )
        version = store.create_version(
            p, script, source_type="generation", label="初始生成",
            notes=f"生成模式：{artifacts.get('mode', 'n/a')}", set_current=True,
        )
        _report("script", f"剧本初稿完成：{len(script.scenes)} 场（版本 {version.id}）")
        return {
            "stage": "script", "script_version_id": version.id,
            "notes": [f"剧本：新初稿 {version.id}，{len(script.scenes)} 场"],
        }

    # ---- 阶段 2：导演拆解 ----

    def director_node(state: ProductionState) -> dict[str, Any]:
        _report("director", "导演拆解镜头…")
        p = _project(state)
        version = store.latest_version(p)
        if version is None:  # 状态图内不该发生（script 先行），防御一下
            raise ValueError("没有剧本版本，无法拆解")
        script = version.script
        existing = store.get_version_breakdown(version.id)
        if existing:
            _report("director", f"已有镜头方案（{len(existing)} 场），直接复用")
            return {
                "stage": "director", "breakdown_count": len(existing),
                "notes": state.get("notes", []) + [f"拆解：复用现有 {len(existing)} 场方案"],
            }
        from ..crew.director import run_director

        style = store.get_video_style(p.id) or {}
        result = run_director(
            llm, script, scene_ids=None,
            adaptation_type=p.adaptation_type,
            visual_style=style.get("visual_style"),
        )
        if not result.success:
            raise ValueError(f"导演拆解失败：{result.summary}")
        shots_data = [shot.model_dump() for bd in result.data for shot in bd.shots]
        store.create_video_version(
            project_id=p.id, shots=shots_data,
            source_type="agent", label="导演拆解", notes=result.summary,
        )
        store.set_version_breakdown(version.id, [bd.model_dump() for bd in result.data])
        _report("director", f"拆解完成：{len(shots_data)} 个镜头")
        return {
            "stage": "director", "breakdown_count": len(shots_data),
            "notes": state.get("notes", []) + [f"拆解：{len(shots_data)} 个镜头"],
        }

    # ---- 阶段 3：美术风格指南 ----

    def style_node(state: ProductionState) -> dict[str, Any]:
        _report("style", "生成视觉风格指南…")
        p = _project(state)
        version = store.latest_version(p)
        script = version.script
        style_now = store.get_video_style(p.id) or {}
        has_anchors = bool(style_now.get("character_appearances")) and bool(
            style_now.get("environment_descriptions")
        )
        if has_anchors:
            _report("style", "已有风格锚（人物/环境），直接复用")
            return {
                "stage": "style",
                "notes": state.get("notes", []) + ["风格：复用现有视觉锚"],
            }
        from ..crew.art_director import run_art_director
        from ..crew.dp import language_for

        # 视觉锚语言与下游提示词一致：从已启用的视频 provider 推断。
        lang = "zh"
        try:
            rows = [r for r in store.list_api_providers(kind="video") if r.enabled]
            if rows:
                lang = language_for(rows[0].name, script.language)
        except Exception:  # noqa: BLE001
            pass
        result = run_art_director(
            llm, script, director_notes="", language=lang,
            visual_style=style_now.get("visual_style"),
        )
        if not result.success:
            raise ValueError(f"风格指南生成失败：{result.summary}")
        # merge 而不是覆盖：人工改过的音色（voice_casting）不能被冲掉。
        # 美术指导不生产定妆图注册表，model_dump 永远带空 reference_images——
        # 浅合并会清空资产库已注册的定妆照（2026-11-11 审计，同 chat/tools/crew.py）。
        patch = result.data.model_dump()
        patch.pop("reference_images", None)
        store.merge_video_style(p.id, patch)
        _report("style", f"风格指南完成：{len(result.data.character_appearances)} 人物锚 / "
                         f"{len(result.data.environment_descriptions)} 环境锚")
        return {
            "stage": "style",
            "notes": state.get("notes", []) + ["风格：新建视觉锚"],
        }

    # ---- 阶段 4：资产库（人物白模 + 场景图）----

    def refs_node(state: ProductionState) -> dict[str, Any]:
        _report("refs", "生成资产库（人物白模 + 场景图）…")
        p = _project(state)
        version = store.latest_version(p)
        script = version.script
        style_raw = store.get_video_style(p.id)
        from ..domain import StyleGuide

        guide = StyleGuide.model_validate(style_raw) if style_raw else None
        existing = (style_raw or {}).get("reference_images") or {}
        if existing:
            _report("refs", f"资产库已有 {len(existing)} 张图，直接复用")
            return {
                "stage": "refs", "refs_count": len(existing),
                "notes": state.get("notes", []) + [f"资产：复用现有 {len(existing)} 张图"],
            }
        from ..media.refs import build_reference_assets

        refs = build_reference_assets(script, guide, on_progress=lambda m: _report("refs", m))
        store.merge_video_style(p.id, {"reference_images": refs})
        _report("refs", f"资产库就绪：{len(refs)} 张图")
        return {
            "stage": "refs", "refs_count": len(refs),
            "notes": state.get("notes", []) + [f"资产：新建 {len(refs)} 张图"],
        }

    # ---- 阶段 5：视频提示词（带资产编号）----

    def prompts_node(state: ProductionState) -> dict[str, Any]:
        _report("prompts", "撰写视频提示词…")
        p = _project(state)
        version = store.latest_version(p)
        script = version.script
        breakdowns_data = store.get_version_breakdown(version.id)
        if not breakdowns_data:
            raise ValueError("没有镜头方案，无法撰写提示词（拆解阶段应已产出）")
        style_data = store.get_video_style(p.id)
        from ..crew.dp import run_dp
        from ..domain import SceneBreakdown, StyleGuide
        from ..video import get_registry

        bds = [SceneBreakdown.model_validate(b) for b in breakdowns_data]
        sg = StyleGuide.model_validate(style_data) if style_data else None

        # 幂等：最新镜头方案里已经有提示词就直接复用（重跑流水线不重复调模型）。
        # 要重新出一版提示词走 generate_video_prompts（剧组域），那里明确是「重写」。
        previous = p.current_video_version_id and store.get_video_version(p.current_video_version_id)
        prev_shots = [s for s in (previous.shots if previous else []) if isinstance(s, dict)]
        if prev_shots and any(s.get("video_prompt") for s in prev_shots):
            _report("prompts", f"提示词已存在（版本 {previous.id}），直接复用")
            return {
                "stage": "prompts", "prompt_version_id": previous.id,
                "notes": state.get("notes", []) + ["提示词：复用现有版本"],
            }

        target_provider, provider_label, max_duration = "", "", 0.0
        try:
            rows = [r for r in store.list_api_providers(kind="video") if r.enabled]
            if rows:
                target_provider = rows[0].name
                prov = get_registry().get_provider(
                    target_provider, api_key=rows[0].api_key, base_url=rows[0].base_url
                )
                if prov:
                    provider_label = prov.label
                    max_duration = prov.max_duration_sec
        except Exception:  # noqa: BLE001
            pass
        result = run_dp(
            llm, script, breakdowns=bds, style_guide=sg,
            target_provider=target_provider, provider_label=provider_label,
            max_duration=max_duration,
        )
        if not result.success:
            raise ValueError(f"提示词生成失败：{result.summary}")
        # 只提交草稿：人工批准前不得进入视频任务队列（既有门禁不变）。
        shots_data = [
            {**s.model_dump(), "prompt_status": "needs_review", "prompt_review_note": ""}
            for s in result.data
        ]
        vv = store.create_video_version(
            project_id=p.id, shots=shots_data,
            source_type="agent", label="生产工作流 · 提示词待审阅",
            notes=f"{result.summary}；逐镜人工批准后才能提交视频任务。",
            parent_version_id=previous.id if previous else None,
        )
        _report("prompts", f"提示词完成：{len(shots_data)} 镜待审（版本 {vv.id}）")
        return {
            "stage": "prompts", "prompt_version_id": vv.id,
            "notes": state.get("notes", []) + [f"提示词：{len(shots_data)} 镜待审"],
        }

    graph = StateGraph(ProductionState)
    graph.add_node("script", script_node)
    graph.add_node("director", director_node)
    graph.add_node("style", style_node)
    graph.add_node("refs", refs_node)
    graph.add_node("prompts", prompts_node)
    graph.add_edge(START, "script")
    graph.add_edge("script", "director")
    graph.add_edge("director", "style")
    graph.add_edge("style", "refs")
    graph.add_edge("refs", "prompts")
    graph.add_edge("prompts", END)
    return graph.compile()


def run_production(
    store: Store,
    llm: LLM,
    settings: Settings,
    project_id: str,
    on_progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """同步跑完整条生产工作流（调用方自己决定放不放进后台线程）。"""
    graph = build_production_graph(store, llm, settings, on_progress=on_progress)
    final: ProductionState = graph.invoke({"project_id": project_id})
    return {
        "project_id": project_id,
        "script_version_id": final.get("script_version_id"),
        "breakdown_count": final.get("breakdown_count", 0),
        "refs_count": final.get("refs_count", 0),
        "prompt_version_id": final.get("prompt_version_id"),
        "notes": final.get("notes", []),
    }
