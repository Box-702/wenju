# =====================================================================
# episode.py —— 按集推进的对话工具（2026-11-27）
#
# 用户要求「严格一集一集往下跑」：这里只暴露状态与前进/回看，
# 真正的完成判据在 app/video/episode_gate.py（确定性，不调模型）。
# =====================================================================

from __future__ import annotations

from langchain_core.tools import tool

from .context import ToolContext


def build(ctx: ToolContext) -> list:
    store = ctx.store

    @tool
    def get_episode_status(project_id: str = "") -> str:
        """查看当前推进到第几集、这一集卡在哪一步（剧本/分镜/提示词批准/出片）。用户问「现在第几集 / 这一集到哪了 / 能不能进下一集」时先调这个。参数：project_id=项目 id（可留空，默认当前作品）。"""
        p = ctx.project(project_id)
        from ...video.episode_gate import active_episode_status

        s = active_episode_status(store, p)
        lines = [
            f"当前第 {s['active']} 集（共 {len(s['episodes'])} 集）",
            f"本集 {s['scenes']} 场 / {s['shots']} 镜 / {s['units']} 个生成单元",
            f"提示词：{'已齐' if s['prompts_ready'] else '未齐'}；"
            f"批准：{'完成' if s['approved'] else '未完成'}；"
            f"出片：{'完成' if s['generated'] else '未完成'}",
        ]
        if s["blockers"]:
            lines.append("卡点：" + "；".join(s["blockers"]))
        if s["complete"] and s["next"]:
            lines.append(f"这一集已完成，可以说「进入第 {s['next']} 集」。")
        elif s["complete"]:
            lines.append("已经是最后一集。")
        return "\n".join(lines)

    @tool
    def next_episode(project_id: str = "") -> str:
        """进入下一集（只在当前集提示词全部批准且全部生成成功后才放行）。用户明确说「进入下一集 / 继续下一集」时调用；被卡住就把卡点原样告诉用户，不要绕过。参数：project_id=项目 id（可留空）。"""
        p = ctx.project(project_id)
        from ...video.episode_gate import advance_episode

        ok, msg, s = advance_episode(store, p)
        if not ok:
            return f"还不能进入下一集：{msg}"
        return f"{msg}。现在只处理第 {s['active']} 集；先拆本集分镜、写提示词、批准并出片。"

    @tool
    def switch_episode(project_id: str, episode: int) -> str:
        """切换到某一集：回看已完成的集随时可以；前进只允许 +1 且当前集已完成，不能跳集。用户说「回第 1 集看看」或明确要进下一集时调用。参数：project_id=项目 id；episode=集号（1 起）。"""
        p = ctx.project(project_id)
        from ...video.episode_gate import switch_episode as _switch

        ok, msg, s = _switch(store, p, int(episode))
        if not ok:
            return msg
        return f"{msg}。当前集 {s['shots']} 镜 / {s['units']} 个生成单元。"

    return [get_episode_status, next_episode, switch_episode]
