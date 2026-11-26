# =====================================================================
# test_source_style.py —— 文言原文识别与台词语言口径（2026-11-25）
#
# 用户反馈：聊斋改编时 Agent 没问白话/文言，剧本一半现代口语一半古文。
# 这里钉死：识别、别名归一、三种口径的提示词块。
# =====================================================================

from __future__ import annotations

from app.pipeline.source_style import (
    classical_score,
    dialogue_style_note,
    is_classical_chinese,
    normalize_dialogue_style,
)

LIAOZHAI = (
    "邑有王生行七，故家子。少慕道，聞勞山多仙人，負笈往遊。"
    "一道士坐蒲團上，素髮垂領，而神觀爽邁。叩而與語，理甚玄妙，請師之。"
    "道士曰：「恐嬌惰不能作苦。」答言：「能之。」"
)
MODERN = (
    "王生在劳山拜一位老道士为师。他原想学仙术，师父却让他天天上山砍柴。"
    "过了两个月，王生的手掌磨出厚茧，终于忍不住请求回家。他想学穿墙术，"
    "师父教了他口诀，他回家向妻子炫耀，结果一头撞在墙上。"
)


def test_聊斋原文判为文言() -> None:
    assert is_classical_chinese(LIAOZHAI) is True
    classical, modern = classical_score(LIAOZHAI)
    assert classical > modern


def test_现代白话判为非文言() -> None:
    assert is_classical_chinese(MODERN) is False


def test_台词语言别名归一() -> None:
    assert normalize_dialogue_style("白话") == "vernacular"
    assert normalize_dialogue_style("vernacular") == "vernacular"
    assert normalize_dialogue_style("文言") == "classical"
    assert normalize_dialogue_style("古文") == "classical"
    assert normalize_dialogue_style("混合") == "mixed"
    assert normalize_dialogue_style("") == ""
    assert normalize_dialogue_style("乱写") == ""


def test_三种口径都有明确约束() -> None:
    v = dialogue_style_note("vernacular")
    c = dialogue_style_note("classical")
    m = dialogue_style_note("mixed")
    assert "白话" in v and "意译" in v
    assert "文言" in c and "听懂" in c
    assert "白话" in m and "唱词" in m
    assert dialogue_style_note("") == ""
