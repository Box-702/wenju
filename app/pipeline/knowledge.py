# =====================================================================
# knowledge.py —— 题材识别与作者风格画像
#
#   1. 题材识别：按关键词粗判小说题材（用于项目展示）；
#   2. 作者风格：从原文启发式提取的语言风格画像（规则引擎），
#      项目创建时提取一次存入项目 notes，生成链路也会直接复用。
# =====================================================================

from __future__ import annotations

import json
import re
from typing import Any

from ..llm import LLM


# ---------- 题材识别 ----------

_GENRE_KEYWORDS: dict[str, list[str]] = {
    "悬疑": ["悬疑", "推理", "破案", "侦探", "杀人", "失踪", "秘密", "谜", "真相", "案件", "调查", "阴谋", "证据", "凶手"],
    "逆袭": ["逆袭", "打脸", "翻身", "复仇", "崛起", "扮猪吃虎", "废柴", "系统", "碾压", "爽文"],
    "情感": ["爱情", "恋爱", "婚姻", "分手", "心动", "告白", "前任", "虐恋", "暗恋", "重逢", "心动"],
    "家庭": ["家庭", "亲情", "母亲", "父亲", "姐妹", "兄弟", "婆媳", "原生家庭", "和解", "血缘"],
    "都市": ["都市", "职场", "城市", "公司", "加班", "合租", "地铁", "写字楼", "白领", "生意"],
    "奇幻": ["玄幻", "修仙", "魔法", "异能", "穿越", "重生", "神魔", "冒险", "剑", "龙", "秘境"],
}
_DEFAULT_GENRE = "通用"


def detect_genres(raw_text: str, top: int = 2) -> list[str]:
    """按关键词粗判题材，返回命中分数最高的题材列表（至少含「通用」兜底）。"""
    text = (raw_text or "")[:6000]
    scored: list[tuple[int, str]] = []
    for genre, words in _GENRE_KEYWORDS.items():
        score = sum(text.count(w) for w in words)
        if score > 0:
            scored.append((score, genre))
    scored.sort(key=lambda x: x[0], reverse=True)
    genres = [g for _, g in scored[:top]]
    return genres or [_DEFAULT_GENRE]


# ---------- 作者语言风格提取 ----------

_IMAGERY_WORDS = ["像", "仿佛", "如同", "如", "月光", "雨", "风", "影子", "灯光", "雾气", "铁", "黄", "灰", "暗", "湿", "冷"]
_TONE_WORDS = ["冷", "暗", "沉", "静", "阴", "湿", "硬", "钝", "闷", "凉", "荒", "锈"]


def _heuristic_style(raw_text: str) -> dict[str, Any]:
    """纯规则的语言风格画像：句长、对白占比、意象密度、语气词。"""
    text = (raw_text or "").strip()
    sentences = [s.strip() for s in re.split(r"[。！？!?；;\n]", text) if s.strip()]
    total_chars = len(text) or 1
    if not sentences:
        sentences = [text]
    avg_len = sum(len(s) for s in sentences) / len(sentences)
    short_ratio = sum(1 for s in sentences if len(s) <= 12) / len(sentences)
    dialogue_chars = sum(len(m) for m in re.findall(r"[“\"]([^”\"]+)[”\"]", text))
    dialogue_ratio = dialogue_chars / total_chars
    imagery = sum(text.count(w) for w in _IMAGERY_WORDS) / total_chars * 1000
    tone = sum(text.count(w) for w in _TONE_WORDS)

    style_bits: list[str] = []
    if avg_len <= 18:
        style_bits.append("句子短促、节奏快")
    elif avg_len <= 30:
        style_bits.append("句子中等、节奏平稳")
    else:
        style_bits.append("句子偏长、有铺陈感")
    if short_ratio >= 0.4:
        style_bits.append("大量短句制造顿挫")
    if dialogue_ratio >= 0.25:
        style_bits.append("对白占比高、靠对话推动")
    elif dialogue_ratio <= 0.05:
        style_bits.append("对白极少、以叙述为主")
    if imagery >= 2.0:
        style_bits.append("意象/感官描写密集")
    if tone >= 6:
        style_bits.append("整体氛围冷峻、沉郁")
    elif tone >= 3:
        style_bits.append("略带压抑氛围")
    if not style_bits:
        style_bits.append("语言平实直白")
    summary = "、".join(style_bits) + "。"
    return {
        "summary": summary,
        "metrics": {
            "avg_sentence_len": round(avg_len, 1),
            "short_sentence_ratio": round(short_ratio, 2),
            "dialogue_ratio": round(dialogue_ratio, 2),
            "imagery_density": round(imagery, 1),
            "tone_score": int(tone),
        },
    }


def extract_author_style(raw_text: str, *, llm: LLM | None = None, language: str = "zh-CN") -> dict[str, Any]:
    """提取作者语言风格：规则画像 + 可选模型润色。

    返回结构：
      - summary:   一句话总述
      - metrics:   数值指标
      - dimensions: 分维度描述
    """
    profile = _heuristic_style(raw_text)
    m = profile["metrics"]
    dims = {
        "句式": (
            f"平均句长 {m['avg_sentence_len']} 字，短句占比 {round(m['short_sentence_ratio'] * 100)}%"
            + ("，短促有力" if m["short_sentence_ratio"] >= 0.4 else "，句式均衡")
        ),
        "节奏": (
            "句子短促、顿挫感强" if m["short_sentence_ratio"] >= 0.4
            else "节奏平缓、铺陈推进"
        ),
        "对白": (
            f"对白占比 {round(m['dialogue_ratio'] * 100)}%，靠对话推动"
            if m["dialogue_ratio"] >= 0.25
            else "对白较少，以叙述和动作描写为主"
        ),
        "意象": (
            f"意象/感官描写密集（密度 {m['imagery_density']}）"
            if m["imagery_density"] >= 2.0
            else "意象使用克制，偏白描"
        ),
        "氛围": (
            "整体氛围冷峻、沉郁" if m["tone_score"] >= 6
            else "略带压抑氛围" if m["tone_score"] >= 3
            else "氛围中性、平实"
        ),
        "语气": "克制、留白，不直说情绪" if m["dialogue_ratio"] < 0.2 and m["tone_score"] >= 3 else "直白自然",
    }
    profile["dimensions"] = dims
    if llm is not None and llm.available:
        try:
            prompt = (
                "请用 2-4 句话概括这段文本作者的写作风格（句式、节奏、对白、意象、氛围、语气），"
                "只输出风格描述本身，不要输出标题或解释。\n"
                f"规则画像参考：{json.dumps(profile, ensure_ascii=False)}\n"
                f"原文（截取）：\n{(raw_text or '')[:2000]}"
            )
            resp = llm.chat().invoke(
                [
                    {"role": "system", "content": f"你是文学编辑，请使用{language}回答。"},
                    {"role": "user", "content": prompt},
                ]
            )
            text = (resp.content or "").strip()
            if text:
                profile["summary"] = text
        except Exception:  # noqa: BLE001
            pass
    return profile


def format_author_style(raw_text: str, *, llm: LLM | None = None, language: str = "zh-CN") -> str:
    """提取并格式化作者风格为可读文本。"""
    style = extract_author_style(raw_text, llm=llm, language=language)
    lines = [f"作者语言风格：{style.get('summary', '')}"]
    for dim_name, dim_text in (style.get("dimensions") or {}).items():
        lines.append(f"  {dim_name}：{dim_text}")
    return "\n".join(lines)


# 章节/噪声行模式：这些不是书名（骆驼祥子素材首行就是「第1章」）。
_CHAPTER_NOISE = re.compile(
    r"^(第\s*[0-9一二三四五六七八九十百千]+\s*[章节回篇卷部]"
    r"|序\s*言|前\s*言|引\s*子|楔\s*子|后\s*记|尾\s*声|目录|附录|版权|简介)"
)
# 作者/年份行：「鲁迅（1919年3月）」——标题的下一行常是这个，要跳过。
_AUTHOR_LINE = re.compile(r"^[\u4e00-\u9fff]{2,4}\s*[（(].*年")


def extract_title(raw_text: str) -> str:
    """从原文提取书名（确定性兜底，不靠模型自觉）。

    规则（对齐真实素材格式，2026-11-12 用孔乙己/骆驼祥子/13号巷核对）：
      1. 首行 ≤20 字、不是章节/噪声行 → 视为书名（孔乙己.txt 的「孔乙己」）；
      2. 否则跳过作者行与空行，取第一行正文的前 12 字（截到标点）；
      3. 全空 → 「未命名作品」。
    Agent 起的名字优先用；本函数用于它没起/起得离谱时的对照与兜底。
    """
    lines = [ln.strip() for ln in (raw_text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "未命名作品"
    first = lines[0]
    if len(first) <= 20 and not _CHAPTER_NOISE.match(first):
        return first
    # 首行是章节号：往下找作者行后的正文，或直接取正文首句
    for ln in lines:
        if _CHAPTER_NOISE.match(ln) or _AUTHOR_LINE.match(ln):
            continue
        head = re.split(r"[，。！？；：\n]", ln)[0]
        return head[:12] or "未命名作品"
    return first[:12]
