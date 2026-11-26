# =====================================================================
# script_rewrite.py —— 生成后的「白话改写」后处理（2026-11-26）
#
# 用户选了「全部白话」，但模型仍会把原文的「之乎者也」照搬进人物小传和
# 台词（v7 实测：王生/嫦娥/妻三份小传、嫦娥/客乙两句）。这里只做一件事：
#   把文言/半文言的**条目文本**改成现代白话，保留人物、事件、数字、语气，
#   不增删情节；改完逐条校验，改不好就保留原文，交给审计继续亮灯。
#
# 为什么不重新生成整本剧本：重生成会改结构、改分镜、改时长；这里只动
# 出问题的那几行文本，剧本结构/场号/节拍 id 全部不变。
# =====================================================================

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from ..domain import Script
from .source_style import classical_score, strict_classical_score

_CHAR_FIELDS = (
    "description", "visual", "tags", "background", "history",
    "personality", "relationships", "arc", "goal", "motivation",
)
# 引号里的唱词/咒语允许保留原文；审计与改写都只看引号外的字。
_QUOTED_RE = re.compile(r"[「『“\"].*?[」』”\"]")


def _unquoted(text: str) -> str:
    return _QUOTED_RE.sub("", text or "")


class RewriteItem(BaseModel):
    key: str
    text: str


class RewriteResult(BaseModel):
    items: list[RewriteItem] = Field(default_factory=list)


def _needs_rewrite(text: str, *, bio: bool = False) -> bool:
    plain = _unquoted(text)
    strict = strict_classical_score(plain)
    modern = classical_score(plain)[1]
    threshold = 2 if bio else 2
    return strict >= threshold and strict > modern


def collect_rewrite_items(script: Script) -> list[dict[str, str]]:
    """收集需要改白话的条目：人物小传字段 + 对白/OS。"""
    items: list[dict[str, str]] = []
    # 白话项目下**所有非空小传字段**都过一遍：模型常写半文半白，
    # 但虚词密度不够，只按「之乎者也」筛会漏掉「神观爽迈」这类句子。
    for c in script.characters:
        for field in _CHAR_FIELDS:
            value = str(getattr(c, field, "") or "").strip()
            if value and (script.dialogue_style == "vernacular"
                          or _needs_rewrite(value, bio=True)):
                items.append({"key": f"char:{c.id}:{field}", "text": value})
    for sc in script.scenes:
        for b in sc.beats:
            if b.type not in ("dialogue", "os"):
                continue
            line = str(b.line or "").strip()
            if line and _needs_rewrite(line):
                items.append({"key": f"beat:{sc.id}:{b.id}", "text": line})
    return items


def _is_better(old: str, new: str) -> bool:
    """改写后必须更白话、且不能短到丢信息。"""
    if not new.strip():
        return False
    if len(new) < max(4, int(len(old) * 0.3)):
        return False
    old_c, _ = classical_score(_unquoted(old))
    new_c, _ = classical_score(_unquoted(new))
    return new_c < old_c or new_c == 0


def apply_rewrite_items(script: Script, items: list[dict[str, str]]) -> int:
    """把改写结果写回剧本；返回实际替换条数。"""
    chars = {c.id: c for c in script.characters}
    scenes = {sc.id: sc for sc in script.scenes}
    changed = 0
    for item in items:
        key = str(item.get("key") or "")
        new = str(item.get("text") or "").strip()
        if not key or not new:
            continue
        parts = key.split(":", 2)
        if len(parts) != 3:
            continue
        kind, owner, field = parts
        if kind == "char":
            c = chars.get(owner)
            if c is None or field not in _CHAR_FIELDS:
                continue
            old = str(getattr(c, field, "") or "")
            if _is_better(old, new):
                setattr(c, field, new)
                changed += 1
        elif kind == "beat":
            sc = scenes.get(owner)
            if sc is None:
                continue
            for b in sc.beats:
                if b.id != field:
                    continue
                old = str(b.line or "")
                if b.type in ("dialogue", "os") and _is_better(old, new):
                    b.line = new
                    changed += 1
                    if b.type == "dialogue":
                        sc.dialogue = [
                            {"speaker": x.speaker, "line": x.line}
                            for x in sc.beats
                            if x.type == "dialogue" and x.speaker and x.line
                        ]
                break
    return changed


def rewrite_vernacular(llm: Any, script: Script, *, targeted: bool = False) -> tuple[Script, int]:
    """调用 LLM 把条目改白话（最多两轮，第二轮补漏）；失败返回原剧本、0。

    targeted=True 时第一轮也只处理仍有文言标记的条目——用于给已生成的
    剧本补收口，避免把已经白话的人物小传再改一遍。
    """
    if not getattr(llm, "available", False):
        return script, 0
    system = (
        "你在做短剧剧本的文字编辑。把下面每一条文言/半文言文本改写成现代白话口语，"
        "保留人物、事件、数字、称呼、语气和信息量；不要增删情节，不要写成解释，"
        "不要输出原文以外的内容。**每一条都必须返回，key 原样保留，不要漏、不要合并**。"
        "唱词、咒语优先保留原文，并放在「」里，不要意译；其余一律改成现代白话。"
        "只返回 JSON：{\"items\":[{\"key\":\"原 key\",\"text\":\"改写后的白话\"}]}。"
    )
    changed = 0
    for attempt in range(2):
        items = collect_rewrite_items(script)
        if targeted or attempt:
            # 第二轮只补仍有文言标记的条目，避免把已改好的白话再改一遍
            items = [
                it for it in items
                if _needs_rewrite(it["text"], bio=it["key"].startswith("char:"))
            ]
        if not items:
            break
        payload = "\n".join(
            f"{i + 1}. key={it['key']}\n   原文：{it['text']}"
            for i, it in enumerate(items)
        )
        try:
            resp = llm.structured(RewriteResult).invoke([
                {"role": "system", "content": system},
                {"role": "user", "content": payload},
            ])
            result = resp if isinstance(resp, RewriteResult) else RewriteResult.model_validate(resp)
        except Exception:  # noqa: BLE001
            break
        changed += apply_rewrite_items(script, [it.model_dump() for it in result.items])
    return script, changed
