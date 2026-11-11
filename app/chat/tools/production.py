# =====================================================================
# production.py —— 生产域工具：一键跑「小说 → 提示词」整条流水线
#
# 这是「工作流 = 对话 Agent 的一个工具」的落点：用户说「开始制作 /
# 出一版提示词」时调用。流水线本身在 app/agent/production.py（LangGraph
# 状态图），这里只负责启动后台任务并转述进度入口。
# 产出的是**待审提示词**——提交视频生成仍是用户的决定（既有门禁不变）。
# =====================================================================

from __future__ import annotations

from langchain_core.tools import tool

from .context import ToolContext


def build(ctx: ToolContext) -> list:
    """装配生产域工具。"""
    store, llm, runner = ctx.store, ctx.llm, ctx.runner

    @tool
    def start_production(project_id: str) -> str:
        """启动整条生产流水线（后台运行）：初始化剧本 → 导演拆解 → 视觉风格 → 人物白模与场景图（资产库）→ 视频提示词。已完成的阶段自动复用，不会重复花钱。产出的提示词需要逐镜批准后才能出视频。用户说「开始制作 / 一键出提示词 / 跑完整流程」时调用。参数：project_id=项目 id。"""
        from ...agent.production import run_production

        p = ctx.project(project_id)

        def _settings():
            from ...config import get_settings

            return get_settings()

        def _run(task):
            def _progress(stage: str, msg: str) -> None:
                # runner 可能为 None（同步回退路径），update_step 前必须守卫——
                # 此前无守卫，同步跑一旦上报进度就 AttributeError（2026-11-11 审计；
                # 对照 screenplay.py 同款写法）。
                if runner is not None:
                    runner.update_step(task.id, msg, "running")

            return "；".join(run_production(
                store, llm, _settings(), p.id, on_progress=_progress,
            ).get("notes") or [])

        if runner:
            task_id = runner.start("生产流水线（小说→提示词）", _run, project_id=p.id)
            ctx.collector["payloads"].append({
                "type": "sub_agent_started",
                "task_id": task_id,
                "name": "生产流水线",
                "project_id": p.id,
            })
            return (
                "生产流水线已启动：初始化剧本 → 导演拆解 → 视觉风格 → 人物白模与场景图 → 视频提示词，"
                "已完成的阶段会自动复用。完成后提示词进入「待审」状态，"
                "用户逐镜批准后我们再决定什么时候生成视频。"
            )
        # 无 runner（测试 / 本地直跑）：同步执行完再回复——话术必须与后台路径区分，
        # 说「已启动」而实际已经跑完是失实汇报（审计修掉的同类问题）。
        summary = _run(type("_T", (), {"id": "local"})())
        return (
            f"生产流水线已跑完：{summary or '各阶段已就绪'}。"
            "提示词处于「待审」状态，用户可以批准后决定什么时候出片。"
        )

    return [start_production]
