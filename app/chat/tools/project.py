# =====================================================================
# project.py —— 作品域：换作品（当前对话归属哪部作品）
#
# 为什么单独一个域：换作品**不是**创作动作，是「这段对话归谁」的导航动作。
# 以前它是输入框里的一个下拉按钮（点一下直接改前端状态），现在改成
# 「切换作品」技能——用户在对话里引用它，Agent 走技能流程执行
# （app/skills/project-switch/SKILL.md）。
#
# 为什么要工具而不是纯前端按钮：作品归属是**对话的事实**。Agent 知道
# 当前对话在哪部作品下，才能把「这份稿子改一下」这类指令落到对的上下文；
# 反过来，切换动作也该留在对话里（有据可查），而不是悄悄换一个壳。
#
# 两个工具：
#   list_projects    —— 列出全部作品（标题 / 版本数 / 状态），供模型与用户对齐；
#   switch_project   —— 把当前会话改挂到目标作品（历史跟着走），并把本轮
#                       上下文的作品切过去（collector["project_id"]）。
#
# 改挂的语义：会话 id 不变、归属换掉，消息历史跟着进新作品。界面不跳、
# 消息流连续；代价是原作品下不再有这个会话（产品不变式「每部作品一个
# 对话」仍成立）。
# =====================================================================

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import tool

from .context import LONG_TEXT_CHARS, ToolContext

#: 匹配作品标题时忽略的空白与常见分隔符（用户口语里常说「《 孔乙己 》」）。
_STRIP_CHARS = " \t　《》〈〉\"'“”‘’·.。,，、-—_"

#: 作品 id 的形状（gen_id("proj") 的产物）：模型常把 id 混在一句话里传进来。
_ID_RE = re.compile(r"proj_[A-Za-z0-9]+")


def _norm(text: str) -> str:
    """把标题归一化：去掉空白与包裹符号，便于口语匹配。"""
    return "".join(ch for ch in str(text or "") if ch not in _STRIP_CHARS).strip().lower()


def _summaries(ctx: ToolContext) -> list[dict[str, Any]]:
    """作品的简表（标题 / 版本数 / 状态），给模型与用户对齐用。"""
    out: list[dict[str, Any]] = []
    for row in ctx.store.list_project_summaries():
        out.append({
            "id": row.get("id") or "",
            "title": row.get("title") or "",
            "versions": int(row.get("version_count") or 0),
            "status": row.get("status") or "",
        })
    return out


def _format_list(rows: list[dict[str, Any]], *, current_id: str = "") -> str:
    """给模型看的作品清单。"""
    if not rows:
        return "现在一部作品都没有。用户想换作品的话，先用 import_storyboard 建一部。"
    lines = []
    for i, r in enumerate(rows, 1):
        mark = "（当前对话就在这部作品下）" if r["id"] == current_id else ""
        lines.append(
            f"{i}. 《{r['title']}》 id={r['id']} · {r['versions']} 版 · 状态 {r['status']}{mark}"
        )
    return "当前的作品：\n" + "\n".join(lines)


def build_project_tools(ctx: ToolContext) -> list[Any]:
    """装配作品域工具。"""

    @tool
    def list_projects() -> str:
        """列出全部作品（剧名、id、版本数、状态），供用户挑一部继续。

        用在：用户想换作品但没说清是哪一部；或你不确定有哪些作品可选。
        不要凭记忆报作品名——先列出来让用户选。
        """
        rows = _summaries(ctx)
        return _format_list(rows, current_id=ctx.collector.get("project_id") or "")

    @tool
    def switch_project(title: str) -> str:
        """把当前对话换到另一部作品下继续。参数：title=作品标题、关键词或作品 id（用户说的剧名，或你从 list_projects 里拿到的 id）。

        用在：用户明确说要换到某部作品（「切到《孔乙己》」「换到替身那部」）。
        标题对不上或同时有多部匹配时，**不要猜**——工具会回候选，让你列给用户挑。
        换完只回执一句「已切到《X》」，不要顺手开始生成任何内容。
        """
        want = _norm(title)
        if not want:
            return "没听清要切到哪一部作品。问清楚剧名，或者用 list_projects 把作品列出来让用户挑。"

        rows = _summaries(ctx)
        if not rows:
            return "现在一部作品都没有，无法切换。先用 import_storyboard 建一部再来。"

        # 先按 id 认（模型从 list_projects 拿到 id 后常直接传 id，或把 id 混在
        # 一句话里——2026-11-22 实测：只认标题的话，同名作品场景下模型会卡在
        # 「按 id 切不了、按标题切怕错」）。
        m = _ID_RE.search(title or "")
        by_id = [r for r in rows if r["id"] == (m.group(0) if m else "")]
        if len(by_id) == 1:
            hits = by_id
        else:
            # 匹配顺序：标题完全对上 → 标题里包含关键词 → 关键词包含标题。
            exact = [r for r in rows if _norm(r["title"]) == want]
            contains = [r for r in rows if want and want in _norm(r["title"])]
            loose = [r for r in rows if want and _norm(r["title"]) in want]
            hits = exact or contains or loose

        if len(hits) > 1:
            names = "、".join(f"《{r['title']}》（id={r['id']}）" for r in hits)
            return (
                f"有 {len(hits)} 部作品都像「{title}」：{names}。"
                "请让用户报一下 id（或改剧名），把 id 原样传进 title 参数即可精确切换，别自己挑。"
            )
        if not hits:
            return (
                f"没有找到叫「{title}」的作品。现有的是："
                + "、".join(f"《{r['title']}》" for r in rows)
                + "。请让用户从里面挑一部。"
            )

        target = hits[0]
        current_id = ctx.collector.get("project_id") or ""
        if target["id"] == current_id:
            return f"当前对话已经在《{target['title']}》下了，不用切。要接着做什么？"

        # 会话改挂：会话 id 不变、归属换掉，历史跟着进新作品。
        conv_id = ctx.collector.get("conversation_id")
        if conv_id:
            try:
                ctx.store.move_conversation_to_project(conv_id, target["id"])
            except ValueError as e:  # 作品在列表与改挂之间被删了
                return f"切换失败：{e}"
        # 本轮上下文切过去（collector 是 done 事件回传作品 id 的唯一来源）。
        # 不用 ctx.project()——它的语义是「取作品并记为本轮上下文」，
        # 这里要的就是「换归属」，写 collector 更直白。
        ctx.collector["project_id"] = target["id"]
        switched = ctx.store.get_project(target["id"])
        raw_chars = len(switched.raw_text or "") if switched else 0
        ctx.collector["raw_text_chars"] = raw_chars
        ctx.collector["long_novel"] = raw_chars > LONG_TEXT_CHARS
        return (
            f"已切到《{target['title']}》（id={target['id']}，{target['versions']} 版）。"
            "这段对话的历史也跟着过来了。接着做什么？"
        )

    return [list_projects, switch_project]
