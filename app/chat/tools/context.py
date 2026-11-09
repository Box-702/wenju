# =====================================================================
# context.py —— 工具层共享的上下文与小件
#
# 每轮对话构造一个 ToolContext（纯对象装配，无 I/O），所有域的工具闭包
# 通过它访问 store / llm / runner / collector，以及一组跨域复用的取数助手。
# 收在这里的原则：两个以上域都要用的东西才进来，别把它变成杂物间。
# =====================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from ...skills import list_skills

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注
    from ...agent.tasks import SubAgentRunner
    from ...llm import LLM
    from ...store import Project, Store


# 超过这个字数的原文视为「长篇」：一次剧本生成（12 个切片 × 约 1200 字）
# 只能覆盖开头一小段，必须诚实告知并引导走「建图 → 选段」。
LONG_TEXT_CHARS = 1200 * 12

# 改编类型 -> 中文名，用于提示模型填参数。产品只做短剧，其余仅兼容老数据。
ADAPT_TYPE_LABELS = {
    "short_drama": "短剧",
    "other": "自定义",
}


class Collector(TypedDict, total=False):
    """工具通过闭包写入的运行期收集器（图状态保持干净可序列化）。"""

    project_id: str | None
    conversation_id: str | None
    payloads: list[dict[str, Any]]


@dataclass
class ToolContext:
    """一轮对话的工具上下文。工具闭包只经它取依赖。"""

    store: Store
    llm: LLM
    collector: Collector
    runner: SubAgentRunner | None = None
    # 本轮对话所属的作品（对话层知道）。工具的 project_id 缺省就用它——
    # 模型偶尔会漏传 project_id（只给关键词），直接失败会让它放弃并反问用户。
    current_project_id: str | None = None

    # ---- 取数助手 ----

    def project(self, project_id: str) -> Project:
        """取项目并把它记为本轮上下文（之后 remember/recall 按它定位）。"""
        p = self.store.get_project(project_id)
        if p is None:
            raise ValueError(f"项目不存在：{project_id}")
        self.collector["project_id"] = project_id
        return p

    def overview_text(self, project: Project) -> str:
        """剧本概况（ask 工具的上下文；不整段灌原文，只给骨架）。"""
        store = self.store
        version = store.latest_version(project)
        if version is None:
            return "（还没有生成剧本版本，可以先让我生成初稿）"
        s = version.script
        scenes = "; ".join(f"{sc.id}:{sc.title}" for sc in s.scenes[:12])
        chars = "; ".join(c.name for c in s.characters[:12])
        return (
            f"标题《{s.title}》\n梗概：{s.logline}\n主题：{'、'.join(s.themes) or '未填写'}\n"
            f"人物：{chars}\n场景：{scenes}"
        )

    def latest_video_version(self, project: Project):
        """最新的镜头方案版本（没有则 None）。"""
        versions = sorted(self.store.list_video_versions(project.id), key=lambda v: v.created_at)
        return versions[-1] if versions else None

    def latest_job_by_shot(self, project_id: str) -> dict[str, Any]:
        """每个镜头最新一次视频任务（后写的覆盖先写的）。"""
        latest: dict[str, Any] = {}
        for j in sorted(self.store.list_video_jobs(project_id), key=lambda x: x.created_at):
            latest[j.shot_id] = j
        return latest


def main_skill_names() -> set[str]:
    """主 Agent 能加载的技能名（供工具校验与测试断言，避免两处硬编码）。"""
    return {m.name for m in list_skills("main")}


def short(text: str, limit: int) -> str:
    """截断展示文本：压平空白，超长加省略号。工具轨迹与逐镜清单共用。"""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
