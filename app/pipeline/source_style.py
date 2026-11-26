# =====================================================================
# source_style.py —— 原文语体判断 + 台词语言口径（2026-11-25）
#
# 用户反馈：拿《聊斋志异》这种文言文改编时，Agent 没问「台词要不要改成
# 白话」就直接写了——结果剧本里一半现代口语、一半「俛首骤入，勿逡巡」。
# 这是一个必须由用户拍板的分叉，不能替作者决定。
#
# 这里只做两件事：
#   1. 轻量判断原文是不是文言文/半文言（给对话工具做硬闸门）；
#   2. 把用户选择（白话/文言/混合）写成注入场景生成的提示词块。
# 真正的询问在对话层用 ask_user_choice 完成；N2D/API 不弹窗，默认沿用原文。
# =====================================================================

from __future__ import annotations

# 文言高频虚词（只收几乎不出现在现代口语里的）
_CLASSICAL_CHARS = "之乎者也矣焉哉曰兮夫盖故乃遂皆莫汝吾尔"
# 严格标记：只收几乎不会在现代口语句子里成对出现的虚词。
# 「也」不进严格表——「怎么也得……也不枉……」是现代口语，会被误判成文言。
_STRICT_CLASSICAL = "之乎者曰矣焉哉兮汝吾尔"
# 现代口语高频词（用来压掉「总之/之后」这类单字误报）
_MODERN_WORDS = (
    "的", "了", "吗", "呢", "吧", "我们", "你们", "他们", "这个", "那个",
    "什么", "怎么", "因为", "所以", "已经", "没有", "一个", "就是", "还是",
)

_STYLE_ALIASES = {
    "vernacular": "vernacular", "白话": "vernacular", "白話": "vernacular",
    "现代": "vernacular", "口语": "vernacular", "baihua": "vernacular",
    "classical": "classical", "文言": "classical", "古文": "classical",
    "古风": "classical", "wenyan": "classical",
    "mixed": "mixed", "混合": "mixed", "半文半白": "mixed", "文白夹杂": "mixed",
}


def classical_score(text: str) -> tuple[int, int]:
    """返回 (文言虚词命中数, 现代口语词命中数)。"""
    raw = text or ""
    classical = sum(raw.count(ch) for ch in _CLASSICAL_CHARS)
    modern = sum(raw.count(w) for w in _MODERN_WORDS)
    return classical, modern


def strict_classical_score(text: str) -> int:
    """严格文言标记命中数（逐句审计用，避免「也」把现代口误判成文言）。"""
    return sum((text or "").count(ch) for ch in _STRICT_CLASSICAL)


def is_classical_chinese(text: str, *, min_hits: int = 5, ratio: float = 2.0) -> bool:
    """粗判文言文/半文言：虚词够多，且明显压过现代口语词。

    例：《聊斋志异》原文（1077 字）之乎者也成片，现代词几乎没有 → True；
    番茄/现代小说「的了吗呢」密集 → False。
    """
    classical, modern = classical_score(text)
    if classical < min_hits:
        # 短选段（如只截了一段聊斋）命中数到不了 5，但密度已经说明问题：
        # 82 字里 4 个之乎者也、0 个现代词，仍然是文言。
        short_but_dense = modern == 0 and classical >= 3 and len(text or "") >= 60
        if not short_but_dense:
            return False
    return classical >= modern * ratio


def normalize_dialogue_style(value: object) -> str:
    """白话/文言/混合 → vernacular/classical/mixed；认不出返回空串。"""
    key = str(value or "").strip().lower()
    return _STYLE_ALIASES.get(key, "")


def dialogue_style_note(style: str) -> str:
    """把台词语言选择写成给场景生成的硬约束；空串返回空。"""
    key = normalize_dialogue_style(style)
    if key == "vernacular":
        return (
            "【台词语言（用户已确认）】全部改成现代白话口语。原文的文言对白要**意译**成"
            "自然口语，不要保留「之乎者也」「俛首骤入」这类句式；专有名词、咒语、唱词"
            "可保留原文，但必须在前后动作/白话里把意思托住。**人物小传、故事梗概也"
            "用白话**，不要照抄原文的文言设定。"
        )
    if key == "classical":
        return (
            "【台词语言（用户已确认）】保留文言/古风台词，允许之乎者也与四字句；"
            "但必须让现代观众听懂——关键古句用动作、前后白话或画面结果把意思交代清楚，"
            "不要连续三句以上纯文言。"
        )
    if key == "mixed":
        return (
            "【台词语言（用户已确认）】叙述与日常对白用现代白话；仪式、唱词、关键古句"
            "可保留文言。文白切换要跟人物/场合绑定（例如道士说文言、王生说白话），"
            "不要同一人物一句话里文白混杂。"
        )
    return ""
