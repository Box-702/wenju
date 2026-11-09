# =====================================================================
# chat/tools —— 对话工具的「实现层」
#
# conductor.py 只保留编排（系统提示、对话图、轮次服务）；每个工具的实现
# 按域拆在这里，一个文件一个域：
#
#   screenplay.py   剧本域：建项目 / 章节地图 / 选段 / 初稿 / 改编 / 咨询
#   source.py       原文域：读原文 / 在原文里搜关键词
#   production.py   生产域：一键跑「小说 → 提示词」整条流水线
#   crew.py         剧组域：拆解 / 风格指南 / 定妆图 / 视频提示词（后台任务）
#   video.py        出片域：批准 / 提交 / 重跑 / 素材 / 合成 / 查进度
#   memory_tools.py 轻件域：记忆（remember/recall）与技能加载（load_skill）
#
# 工具都是闭包工厂：`build(ctx)` 返回一组绑定上下文的 @tool。
# 跨域共享的小件（项目上下文、状态标签、文本截断）收在 context.py。
# =====================================================================

from . import asset, crew, memory_tools, production, screenplay, source, video
from .context import Collector, ToolContext, short


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
    return [
        *memory_tools.build(ctx),
        *screenplay.build(ctx),
        *source.build_source_tools(ctx),
        *asset.build_asset_tools(ctx),
        *production.build(ctx),
        *crew.build(ctx),
        *video.build(ctx),
    ]


__all__ = ["Collector", "ToolContext", "build_chat_tools", "short"]
