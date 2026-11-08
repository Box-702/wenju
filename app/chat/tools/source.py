# =====================================================================
# source.py —— 读原文的工具
#
# 为什么要有：原文 → 初稿 是整条管线里最黑的一段。Agent 读不到原文，
# 就没法把初稿和原文对照——初稿改错了，只能靠用户自己比对，
# 连「模型是不是读漏了」都判断不了。
#
# 只读不改：这两个工具不改原文、不改剧本，只把原文摆出来。
# =====================================================================
from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from .context import ToolContext

# 一次最多给多少字：一次塞太多会把上下文冲爆，也会让模型抓不住重点。
MAX_CHARS = 4000
# 关键词命中最多回多少条。
MAX_HITS = 6
# 每条命中的上下文长度（前后各这么多字）。
HIT_WINDOW = 160


def _source_text(ctx: ToolContext, project_id: str) -> tuple[Any, str]:
    p = ctx.project(project_id)
    text = (getattr(p, "raw_text", "") or "").strip()
    return p, text


def build_source_tools(ctx: ToolContext) -> list[Any]:
    @tool
    def read_source(project_id: str, start: int = 0, chars: int = MAX_CHARS) -> str:
        """读这部作品的原著原文（分段读）。参数：project_id=项目 id，start=从第几个字开始（默认 0），
        chars=读多少字（默认 4000，最多 4000）。

        用途：核对初稿与原文是否对得上、回答「原文里第几段写了什么」。
        原文很长时按 start 往下翻；工具会告诉你总字数和还剩多少。
        """
        p, text = _source_text(ctx, project_id)
        if not text:
            return f"《{p.title}》还没有原文（导入时的原文是空的）。"
        total = len(text)
        start = max(0, int(start or 0))
        if start >= total:
            return f"《{p.title}》原文共 {total} 字，从第 {start} 字起已到末尾。"
        take = max(200, min(int(chars or MAX_CHARS), MAX_CHARS))
        chunk = text[start:start + take]
        end = start + len(chunk)
        tail = f"（后面还有 {total - end} 字）" if end < total else "（已到末尾）"
        return (
            f"《{p.title}》原文共 {total} 字。以下是第 {start}~{end} 字{tail}：\n"
            f"———\n{chunk}\n———\n"
            f"继续读请把 start 设为 {end}。"
        )

    @tool
    def search_source(project_id: str, keyword: str, window: int = HIT_WINDOW) -> str:
        """在原著原文里按关键词定位（找出某段话出现在原文哪里）。参数：project_id=项目 id，
        keyword=要找的关键词或短语，window=每条命中带多少字上下文（默认 160）。

        用途：用户问「原文里怎么写的 XXX」时，直接定位原文出处；
        也用来核对初稿有没有偏离原文。
        """
        p, text = _source_text(ctx, project_id)
        kw = (keyword or "").strip()
        if not kw:
            return "要搜什么？给个关键词。"
        if not text:
            return f"《{p.title}》还没有原文。"
        win = max(40, min(int(window or HIT_WINDOW), 600))
        hits: list[str] = []
        pos = 0
        while len(hits) < MAX_HITS:
            i = text.find(kw, pos)
            if i < 0:
                break
            lo = max(0, i - win)
            hi = min(len(text), i + len(kw) + win)
            frag = text[lo:hi].replace("\n", " ")
            hits.append(f"第 {i}~{hi} 字：…{frag}…")
            pos = i + len(kw)
        if not hits:
            return f"《{p.title}》原文里没有出现「{kw}」。"
        more = "" if len(hits) < MAX_HITS else f"（还有更多命中，只列前 {MAX_HITS} 条）"
        return f"《{p.title}》原文共 {len(text)} 字，命中「{kw}」{len(hits)} 处{more}：\n" + "\n".join(hits)

    return [read_source, search_source]
