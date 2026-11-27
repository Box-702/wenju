# =====================================================================
# chat/tools —— 对话工具的「实现层」
#
# conductor.py 只保留编排（系统提示、对话图、轮次服务）；每个工具的实现
# 按域拆在这里，一个文件一个域：
#
#   screenplay.py   剧本域：建项目 / 章节地图 / 选段 / 初稿 / 改编 / 咨询
#   project.py      作品域：换作品（list_projects / switch_project）
#   source.py       原文域：读原文 / 在原文里搜关键词
#   script_reader.py 作品文档域：read_script（剧本台本 / 目录 / 镜头 / 视觉资产）
#   production.py   生产域：一键跑「小说 → 提示词」整条流水线
#   crew.py         剧组域：拆解 / 风格指南 / 定妆图 / 视频提示词（后台任务）
#   video.py        出片域：批准 / 提交 / 重跑 / 素材 / 合成 / 查进度
#   confirm.py     确认域：流程分叉确认卡（ask_user_choice，D-76）
#   blockout.py     白模域：施工单起草调整 / 后台渲染 / 查结果（D-54）
#   memory_tools.py 轻件域：记忆（remember/recall）与技能加载（load_skill）
#
# 工具都是闭包工厂：`build(ctx)` 返回一组绑定上下文的 @tool。
# 跨域共享的小件（项目上下文、状态标签、文本截断）收在 context.py。
# =====================================================================

from . import (
    asset,
    blockout,
    confirm,
    crew,
    episode,
    memory_tools,
    production,
    project,
    screenplay,
    script_reader,
    source,
    video,
)
from .context import Collector, LONG_TEXT_CHARS, ToolContext, short


def build_chat_tools(
    store,
    llm,
    collector: Collector,
    runner=None,
    project_id: str | None = None,
) -> list:
    """构造对话 conductor 的工具集。conductor.build_chat_tools 只转调这里。

    project_id：本轮对话所属的作品——工具里 project_id 漏传时的兜底。
    """
    ctx = ToolContext(
        store=store, llm=llm, collector=collector, runner=runner,
        current_project_id=project_id,
    )
    if project_id:
        current = store.get_project(project_id)
        if current is not None:
            raw_chars = len(current.raw_text or "")
            collector["raw_text_chars"] = raw_chars
            collector["long_novel"] = raw_chars > LONG_TEXT_CHARS
    tools = [
        *memory_tools.build(ctx),
        *screenplay.build(ctx),
        *project.build_project_tools(ctx),
        *source.build_source_tools(ctx),
        *script_reader.build(ctx),
        *asset.build_asset_tools(ctx),
        *blockout.build_blockout_tools(ctx),
        *production.build(ctx),
        *crew.build(ctx),
        *episode.build(ctx),
        *video.build(ctx),
        *confirm.build(ctx),
    ]
    # Runtime capability declarations: the loop exposes gated tools only after
    # their workflow skill has been loaded in this conversation turn.
    required_skills = {
        "create_project": "script-generation", "generate_script": "script-generation",
        "map_novel": "novel-serial", "propose_arcs": "novel-serial",
        # plan_story / confirm_story_plan 不再只挂在 novel-serial：
        # 短剧默认流程也要先出「理解卡 + 分集提案」，否则分集大纲永远是空的。
        "run_adaptation": "script-review", "run_script_doctor": "script-review",
        "breakdown_scenes": "crew-pipeline", "generate_style_guide": "crew-pipeline",
        "generate_video_prompts": "crew-pipeline",
        "generate_reference_assets": "asset-generation", "save_asset": "asset-generation",
        "create_location_state": "asset-generation",
        "select_location_state": "asset-generation",
        "clone_voice": "asset-generation", "attach_asset": "asset-generation",
        "start_production": "production-delivery", "import_storyboard": "production-delivery",
        "approve_video_prompts": "production-delivery", "submit_videos": "production-delivery",
        "generate_video": "production-delivery", "rerun_video": "production-delivery",
        "edit_shot_prompt": "production-delivery", "repair_prompt_dialogue": "production-delivery",
        "replace_in_prompts": "production-delivery", "cancel_video_job": "production-delivery",
        "finalize_film": "production-delivery",
        "next_episode": "production-delivery", "switch_episode": "production-delivery",
        "blockout_sheet_save": "blockout",
        "blockout_build": "blockout", "blockout_check": "blockout",
        "switch_project": "project-switch",
    }
    for item in tools:
        required = required_skills.get(getattr(item, "name", ""))
        if required:
            metadata = getattr(item, "metadata", None)
            if metadata is None:
                item.metadata = {"required_skill": required}
            else:
                metadata["required_skill"] = required
    by_name = {getattr(item, "name", ""): item for item in tools}
    if "generate_script" in by_name:
        metadata = getattr(by_name["generate_script"], "metadata", None)
        if metadata is None:
            metadata = {}
        metadata["conditional_skill"] = "novel-serial"
        by_name["generate_script"].metadata = metadata
    return tools


__all__ = ["Collector", "ToolContext", "build_chat_tools", "short"]
