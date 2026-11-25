# =====================================================================
# casting.py —— 选角：把「角色」映射到「音色」
#
# 解决什么：成片里男角色张嘴却出女声（2026-10-31《孔乙己》实测）。
# 根因不是 TTS，而是旧的 assign_voices 用一个「男女交替」列表兜底，而且
# 列表**从女声起头**——于是全片第一个推断不出性别的角色（孔乙己）就分到
# 御姐音，后面每个角色再按男女隔一交替。这种「按序号发声音」还有个副作用：
# 同一场里几个男角色共用一把嗓子，观众根本分不清谁在说话。
#
# 现在的规则（按优先级，每一档都记下依据，便于人工推翻）：
#   1. 显式选角    —— 资产库里为该角色存过 voice_id（人工确认过，最高权威）
#   2. 性别字段    —— Character.gender（剧本阶段 LLM 直接标注）
#   3. 称谓线索    —— 名字里含「嫂/母亲/姑娘/夫人…」这类强性别词
#   4. 代词线索    —— 外貌描述里只出现「她」或只出现「他」
#   5. 名用字      —— 翠/娟/婷/芳… 这类高频女性名用字（弱证据，标记为推测）
#   6. 默认        —— **男声**。中文短剧里绝大多数角色是男性，而且「女声从
#                    男人嘴里出来」比「女角色用了男声」刺耳得多；宁可默认
#                    偏保守，也要把这条写进 warnings 让用户看见、可改。
#
# 同一性别内按出场顺序轮换不同音色，保证「一场里两个男角色不是同一把嗓子」。
# =====================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..domain import Script, normalize_gender, normalize_visual_style
from .assets import build_index, lookup, rekey
from ..llm.tts import (
    VOICE_FEMALE_MATURE,
    VOICE_FEMALE_YOUNG,
    VOICE_MALE_MATURE,
    VOICE_MALE_YOUNG,
)

log = logging.getLogger(__name__)

# ---- 音色池 ------------------------------------------------------------
# 只收 MiniMax 官方系统音色。池内顺序 = 分配优先级（先给「有辨识度」的音色）。
VOICE_MALE_ELITE = "male-qn-jingying"      # 精英青年
VOICE_MALE_STUDENT = "male-qn-daxuesheng"  # 青年学生
VOICE_FEMALE_SOFT = "female-chengshu"      # 成熟女性
VOICE_FEMALE_SWEET = "female-tianmei"      # 甜美女性

MALE_POOL: tuple[str, ...] = (
    VOICE_MALE_MATURE, VOICE_MALE_YOUNG, VOICE_MALE_ELITE, VOICE_MALE_STUDENT,
)
FEMALE_POOL: tuple[str, ...] = (
    VOICE_FEMALE_MATURE, VOICE_FEMALE_YOUNG, VOICE_FEMALE_SOFT, VOICE_FEMALE_SWEET,
)

# 漫剧（二次元）的池子顺序不同：年轻、清亮优先，磁性/御姐往后放。
# 同样是「男主」，真人都市短剧要成熟压场，漫剧的少年主角要清亮——共用一套
# 分配顺序会让漫剧听起来像配音棚里的中年话剧腔。
MALE_POOL_ANIME: tuple[str, ...] = (
    VOICE_MALE_YOUNG, VOICE_MALE_STUDENT, VOICE_MALE_ELITE, VOICE_MALE_MATURE,
)
FEMALE_POOL_ANIME: tuple[str, ...] = (
    VOICE_FEMALE_YOUNG, VOICE_FEMALE_SWEET, VOICE_FEMALE_MATURE, VOICE_FEMALE_SOFT,
)


def voice_pools(visual_style: str | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """按画风给 (男声池, 女声池)。

    画风影响配音气质：二次元/三维动画的角色不会用中年话剧腔，所以池子顺序
    换成年轻清亮优先；实拍与水墨用写实默认。这不是「另一个类型」，只是同一套
    池子的优先级不同——漫剧仍是短剧，节奏与导演逻辑完全共用。
    """
    return (MALE_POOL_ANIME, FEMALE_POOL_ANIME) if normalize_visual_style(visual_style) in {"anime", "3d"} else (MALE_POOL, FEMALE_POOL)

# 音色中文标签（前端下拉、报告里都用它，避免把 voice_id 直接甩给用户）。
VOICE_LABELS: dict[str, str] = {
    VOICE_MALE_MATURE: "磁性男声",
    VOICE_MALE_YOUNG: "青涩男声",
    VOICE_MALE_ELITE: "精英男声",
    VOICE_MALE_STUDENT: "学生男声",
    VOICE_FEMALE_MATURE: "御姐音",
    VOICE_FEMALE_YOUNG: "少女音",
    VOICE_FEMALE_SOFT: "成熟女声",
    VOICE_FEMALE_SWEET: "甜美女声",
}

# 某个音色在厂商侧不可用时的退路（换到同性别里最稳的那个）。
VOICE_FALLBACK: dict[str, str] = {
    VOICE_MALE_ELITE: VOICE_MALE_MATURE,
    VOICE_MALE_STUDENT: VOICE_MALE_YOUNG,
    VOICE_FEMALE_SOFT: VOICE_FEMALE_MATURE,
    VOICE_FEMALE_SWEET: VOICE_FEMALE_YOUNG,
}


def gender_of_voice(voice_id: str) -> str:
    """音色 → 性别（male / female / unknown）。"""
    v = str(voice_id or "").strip().lower()
    if v.startswith("male"):
        return "male"
    if v.startswith("female"):
        return "female"
    return "unknown"


# ---- 性别推断 ----------------------------------------------------------

# 强线索：出现在**名字**里基本可以定性（只查名字，不查描述——描述里
# 「她的父亲」会把父亲判成女的）。
_FEMALE_TITLE_HINTS = (
    "女", "母", "妈", "娘", "姐", "妹", "姑", "姨", "婆", "嫂", "婶",
    "妻", "妇", "妃", "公主", "太后", "夫人", "太太", "小姐", "姑娘",
)
_MALE_TITLE_HINTS = (
    "男", "父", "爸", "爹", "叔", "伯", "爷", "兄", "弟", "哥", "郎",
    "先生", "公子", "少爷", "老爷", "和尚", "道士", "和尚",
)

# 弱线索：高频女性名用字。只在前面几档都判不出来时才用。
_FEMALE_NAME_CHARS = set("翠芳兰娟丽婷燕霞玲敏静梅珍莲桃娥媛薇娜蓉秀英慧琴雪莉倩莹馨")

_NOISE = set(" \t\r\n　·•-—_()（）[]【】「」、，。.·")


def _norm(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch not in _NOISE)


def infer_gender(
    name: str,
    *,
    role: str | None = None,
    personality: str | None = None,
    appearance: str | None = None,
) -> tuple[str, str, str]:
    """推断角色性别，返回 ``(male|female|unknown, 依据, 来源)``。

    依据是给人看的短句（前端「资产库」直接显示，用户一眼知道系统凭什么
    这么判、要不要改）；来源供程序区分证据强度。
    """
    n = _norm(name)

    for hint in _FEMALE_TITLE_HINTS:
        if hint in n:
            return "female", f"名字含「{hint}」", "name"
    for hint in _MALE_TITLE_HINTS:
        if hint in n:
            return "male", f"名字含「{hint}」", "name"

    # 代词线索：外貌/人设描述只会用「他」或「她」指代本角色。
    text = f"{appearance or ''} {personality or ''} {role or ''}"
    has_she = "她" in text
    has_he = "他" in text
    if has_she and not has_he:
        return "female", "设定描述用「她」指代", "pronoun"
    if has_he and not has_she:
        return "male", "设定描述用「他」指代", "pronoun"

    if any(ch in _FEMALE_NAME_CHARS for ch in n):
        hit = next(ch for ch in n if ch in _FEMALE_NAME_CHARS)
        return "female", f"名字用字「{hit}」偏女性（推测）", "guess"

    return "unknown", "无性别线索", "default"


# ---- 选角结果 ----------------------------------------------------------


@dataclass
class CastMember:
    """一个角色的选角结果。"""

    name: str
    char_id: str = ""
    gender: str = "unknown"       # male / female / unknown
    voice_id: str = VOICE_MALE_MATURE
    basis: str = ""               # 分配依据（给人看）
    source: str = "default"       # casting / gender / name / pronoun / guess / default
    appearance: str = ""
    image: str = ""               # 定妆图 URL（资产库展示用）
    label_hint: str = ""          # 发现音色带来的显示名（内置音色留空走 VOICE_LABELS）

    @property
    def voice_label(self) -> str:
        # 内置音色优先用我们起的中文名（磁性男声…）；发现/勾选的音色不在内置表里，
        # 用厂商名（label_hint）；都没有才退回原始 id。顺序不能反，否则内置音色的
        # 中文标签会被厂商名覆盖（回归）。
        return VOICE_LABELS.get(self.voice_id) or self.label_hint or self.voice_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "char_id": self.char_id,
            "gender": self.gender,
            "voice_id": self.voice_id,
            "voice_label": self.voice_label,
            "basis": self.basis,
            "source": self.source,
            "appearance": self.appearance,
            "image": self.image,
        }


@dataclass
class Casting:
    """整部剧的选角表。"""

    members: list[CastMember] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 陌生角色的兜底池（漫剧项目要跟着换成年轻音色池）
    male_pool: tuple[str, ...] = MALE_POOL
    # voice_id → 显示名（发现/勾选的音色带来的）。内置音色不在这里，走 VOICE_LABELS。
    # 配音进度、成片报告都靠它把 clever_boy 显示成「聪明男童」，而不是原始 id（D-24）。
    voice_names: dict[str, str] = field(default_factory=dict)

    def label_for(self, voice_id: str) -> str:
        """一个 voice_id 的中文名：内置表 → 发现名 → 原始 id。"""
        return VOICE_LABELS.get(voice_id) or self.voice_names.get(voice_id) or voice_id

    @property
    def voices(self) -> dict[str, str]:
        """{角色名: voice_id}。"""
        return {m.name: m.voice_id for m in self.members}

    @property
    def voices_by_id(self) -> dict[str, str]:
        """{char_id: voice_id}——剧本节拍的 speaker 存的是 id，两种键都要能用。"""
        return {m.char_id: m.voice_id for m in self.members if m.char_id}

    def voice_for(self, speaker: str, alias: str | None = None) -> str:
        """按名字或 id 取音色；查不到时用确定性的兜底（不静默塞同一把嗓子）。"""
        by_name = self.voices
        by_id = self.voices_by_id
        for key in (speaker, alias):
            if not key:
                continue
            if key in by_name:
                return by_name[key]
            if key in by_id:
                return by_id[key]
        # 别名兜底：「酒客甲」→「酒客」，或反向包含。
        target = _norm(alias or speaker)
        for m in self.members:
            n = _norm(m.name)
            if n and (n in target or target in n):
                return m.voice_id
        # 完全陌生的人（剧本里临时冒出来的群演）：按名字稳定散列到男声池，
        # 保证全片同一人始终同一把嗓子，而不是全挤到 VOICE_MALE_MATURE。
        pool = self.male_pool or MALE_POOL
        return pool[sum(ord(c) for c in target) % len(pool)]


def _pick_from_pool(pool: Iterable[str], used: set[str], fallback: str) -> str:
    """池内取一个还没被占用的音色；全占满则回到池尾。

    为什么是池尾而不是池首：池首是最有辨识度的音色，已经给了戏份最重的人。
    溢出的人（群演、只有一两句的配角）复用池尾，至少不会和主角同一把嗓子——
    观众听不出「路人和主角声音一样」的机率，比听不出两个路人的低得多。
    """
    pool = tuple(pool)
    for v in pool:
        if v not in used:
            return v
    return pool[-1] if pool else fallback


# 音色池按「谁先挑」排序：主角先拿到最有辨识度的音色，群演最后捡剩下的。
_ROLE_PRIORITY = {
    "protagonist": 0, "antagonist": 1, "supporting": 2,
    "mentor": 3, "foil": 4, "other": 5,
}


# ---- 年龄分池（2026-11-25） -------------------------------------------
# 事故背景：《劳山道士》六十余岁的老道士拿到「青年大学生音色」，二十岁上下的
# 妻子拿到「少女音色」——挑音色只看性别与戏份，完全没读角色年龄。这里补一条
# 纯规则：能从外貌/设定里读出年龄，就把池子里「音色气质」更接近年龄段的挪到
# 前面（只重排池内成员，不新增音色；读不出年龄时行为与以前完全一致）。
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

# 没有阿拉伯数字时，这些词本身就是年龄信号（映射到该段位的代表年龄）。
_AGE_WORDS = (
    (("年迈", "老翁", "老者", "老太", "白发", "白须", "白眉", "花甲", "古稀", "晚年"), 65),
    (("少年", "少女"), 15),
)


def _cn_number(text: str) -> int | None:
    """解析「六十 / 二十 / 三十五 / 十八」这类中文数字；失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


def age_from_text(text: str | None) -> int | None:
    """从角色设定/外貌描述里读年龄（纯规则，读不到返回 None）。

    认：阿拉伯数字（38 岁）、中文数字（六十余岁 / 二十上下 / 三十五岁）、
    「年过六旬 / 七旬」与老者/白须/少年这类强年龄词。
    """
    raw = str(text or "")
    if not raw:
        return None
    import re

    m = re.search(r"(\d{1,2})\s*(?:岁|周岁)", raw)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = re.search(r"([一二三四五六七八九十]{1,3})\s*(?:余|多|来)?\s*(?:岁|周岁)", raw)
    if m:
        age = _cn_number(m.group(1))
        if age is not None:
            return age
    m = re.search(r"年过([一二三四五六七八九十]{1,2}|半)旬", raw)
    if m:
        if m.group(1) == "半":
            return 55
        age = _cn_number(m.group(1))
        if age is not None:
            return age * 10 if age <= 9 else age
    m = re.search(r"半百", raw)
    if m:
        return 50
    m = re.search(r"([一二三四五六七八九十]{1,2})旬", raw)
    if m:
        age = _cn_number(m.group(1))
        if age is not None:
            return age * 10 if age <= 9 else age
    for words, age in _AGE_WORDS:
        if any(w in raw for w in words):
            return age
    return None


def _age_preferred_pool(
    pool: tuple[str, ...], gender: str, age: int | None,
) -> tuple[str, ...]:
    """按年龄段把池子里气质更合适的音色挪到前面（只重排，不增删）。

    男声：老年→磁性优先；中年→精英/磁性优先。
    女声：老年→成熟女性优先；中年→御姐/成熟优先。
    青年/少年段与读不出年龄时保持原顺序（漫剧少年主角要清亮，这是原设计）。
    """
    if age is None or not pool:
        return pool
    if gender == "female":
        if age >= 50:
            preferred = (VOICE_FEMALE_SOFT, VOICE_FEMALE_MATURE)
        elif age >= 30:
            preferred = (VOICE_FEMALE_MATURE, VOICE_FEMALE_SOFT)
        else:
            return pool
    else:
        if age >= 50:
            preferred = (VOICE_MALE_MATURE, VOICE_MALE_ELITE)
        elif age >= 35:
            preferred = (VOICE_MALE_ELITE, VOICE_MALE_MATURE)
        else:
            return pool
    head = [v for v in preferred if v in pool]
    if not head:
        return pool
    return tuple(head + [v for v in pool if v not in head])


def _cast_order(script: Script) -> list:
    """角色挑音色的顺序：主角 → 反派 → 配角 → 其余，同级按台词字数降序。

    为什么要排序：《孔乙己》的剧本里主角排在第 4 个出场，按出场顺序分音色
    会把「学生男声」给主角、把「磁性男声」给一个只有一句词的酒客。
    为什么还要看台词量：剧本阶段常把所有人一律标成 supporting（实测整部
    《孔乙己》五个角色全是 supporting），只按 role 排等于没排；戏份最重的
    人先挑音色，是比 role 更可靠的信号。排序稳定，同片内仍然确定。
    """
    speech = _speech_chars(script)
    return sorted(
        script.characters,
        key=lambda c: (
            _ROLE_PRIORITY.get(c.role, 5) if isinstance(c.role, str) else 5,
            -speech.get(c.id, 0),
        ),
    )


def _speech_chars(script: Script) -> dict[str, int]:
    """统计每个角色说了多少字（台词量 ≈ 戏份权重）。"""
    counts: dict[str, int] = {}
    for sc in script.scenes:
        for b in sc.beats or []:
            if b.type == "dialogue" and b.speaker:
                counts[b.speaker] = counts.get(b.speaker, 0) + len(b.line or "")
    return counts


def _resolve_pools(
    voice_pool: list[dict[str, Any]] | None,
    default_male: tuple[str, ...],
    default_female: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """把「勾选启用的音色」拆成男/女自动选角池 + voice_id→显示名。

    空池（没勾选过）→ 原样回退内置男/女池，行为与改造前完全一致。
    gender=other 的音色不进自动池（自动选角依赖性别分池，猜性别是出过事故的）——
    它们仍出现在下拉里，只是只能由人手动指定给某个角色。
    """
    rows = [r for r in (voice_pool or []) if isinstance(r, dict) and r.get("voice_id")]
    if not rows:
        return default_male, default_female, {}
    male = tuple(str(r["voice_id"]) for r in rows if r.get("gender") == "male")
    female = tuple(str(r["voice_id"]) for r in rows if r.get("gender") == "female")
    names = {str(r["voice_id"]): str(r.get("name") or r["voice_id"]) for r in rows}
    return male, female, names


def cast_script(
    script: Script,
    *,
    casting: dict[str, str] | None = None,
    genders: dict[str, str] | None = None,
    appearances: dict[str, str] | None = None,
    images: dict[str, str] | None = None,
    visual_style: str | None = None,
    voice_pool: list[dict[str, Any]] | None = None,
) -> Casting:
    """为整部剧选角。

    ``casting``  资产库里人工确认过的 {角色名: voice_id}，最高优先级；
    ``genders``  资产库里的 {角色名: male/female}（人工修正覆盖推断）；
    ``appearances`` / ``images`` 来自美术指导与定妆图，仅用于展示与推断；
    ``visual_style`` 决定音色池顺序（二次元/三维用年轻音色池）；
    ``voice_pool``  BYOK 勾选启用的音色 [{voice_id, name, gender}]。给了就用它做
    自动选角池（按 gender 分男/女；other 不参与自动），没给回退内置硬编码池——
    老项目与测试都跑在「没勾选」上，行为完全不变。
    """
    casting = {k: v for k, v in (casting or {}).items() if v}
    genders = {k: normalize_gender(v) for k, v in (genders or {}).items()}
    appearances = appearances or {}
    images = images or {}
    # 资产表可能是按名字存的（老项目）也可能是按 id 存的（新数据）：统一成 id 键，
    # 之后一律用 char_id 取。名字变了不会丢锚点，这是「长篇不跑偏」的前提。
    idx = build_index(script)
    casting = rekey(casting, idx)
    genders = rekey(genders, idx)
    appearances = rekey(appearances, idx)
    images = rekey(images, idx)
    default_male, default_female = voice_pools(visual_style)
    male_pool, female_pool, voice_names = _resolve_pools(
        voice_pool, default_male, default_female,
    )

    result = Casting(male_pool=male_pool, voice_names=voice_names)
    used_male: set[str] = set()
    used_female: set[str] = set()
    guessed: list[str] = []

    for c in _cast_order(script):
        look = lookup(appearances, c.id, idx)
        gender, basis, source = infer_gender(
            c.name,
            role=c.role,
            personality=c.personality,
            appearance=look,
        )
        # 角色自带的 gender 字段（剧本阶段标注）比推断可靠，覆盖之。
        char_gender = normalize_gender(getattr(c, "gender", None))
        if char_gender != "unknown":
            gender, basis, source = char_gender, "剧本标注", "gender"
        if genders.get(c.id) in ("male", "female"):
            gender, basis, source = genders[c.id], "资产库指定", "casting"
        if gender == "unknown":
            gender = "male"  # 兜底：默认男声，见文件头说明
            basis = (basis + "→ 默认男声") if basis else "默认男声"
            source = "default"
            guessed.append(c.name)

        # 显式选角（人工确认）优先于一切推断，包括性别推断。
        explicit = casting.get(c.id)
        if explicit:
            voice_id = explicit
            source, basis = "casting", "资产库指定音色"
            gender = gender_of_voice(explicit) if gender_of_voice(explicit) != "unknown" else gender
            if gender == "male":
                used_male.add(voice_id)
            elif gender == "female":
                used_female.add(voice_id)
        else:
            age = age_from_text("；".join(
                part for part in (
                    look,
                    str(getattr(c, "description", "") or ""),
                    str(getattr(c, "personality", "") or ""),
                ) if part
            ))
            if gender == "female":
                voice_id = _pick_from_pool(
                    _age_preferred_pool(female_pool, "female", age),
                    used_female, VOICE_FEMALE_MATURE,
                )
                used_female.add(voice_id)
            else:
                voice_id = _pick_from_pool(
                    _age_preferred_pool(male_pool, "male", age),
                    used_male, VOICE_MALE_MATURE,
                )
                used_male.add(voice_id)

        result.members.append(CastMember(
            name=c.name, char_id=c.id, gender=gender, voice_id=voice_id,
            basis=basis, source=source, appearance=look,
            image=lookup(images, c.id, idx),
            label_hint=voice_names.get(voice_id, ""),
        ))

    if guessed:
        result.warnings.append(
            "以下角色没有性别线索，暂按男声配音（可在「资产库」里指定性别与音色）："
            + "、".join(guessed)
        )
    return result


def resolve_casting(store: Any, project_id: str, script: Script) -> Casting:
    """读库选角（纯读）：资产库视图、提交侧音色锚、比对侧共用这一份口径。"""
    style = store.get_video_style(project_id) or {}
    return cast_script(
        script,
        casting=style.get("voice_casting") or None,
        genders=style.get("character_genders") or None,
        appearances=style.get("character_appearances") or None,
        images=style.get("reference_images") or None,
        visual_style=style.get("visual_style"),
        voice_pool=build_voice_pool(store, style.get("visual_style")),
    )


def cast_and_persist(store: Any, project_id: str, script: Script) -> Casting:
    """选角 + **落库**：把自动推断出的音色固化进 voice_casting。

    2026-11-20《替身v2》复盘的音色规范缺口：cast_script 是纯函数，自动选出的
    音色每次现算、从不落库——用户改画风或换勾选池，同一角色下一轮就换了嗓子；
    而 native 出片的音色桥（service._auto_references）要读的正是
    voice_casting 这份**持久**表。人工指定（source=casting）的不动，
    只补「没指定过」的角色——这份表是用户的，代码只填空。

    写回走 merge_video_style（浅合并），与资产库 PATCH / 克隆绑定同一口径；
    失败只记日志不抛出——选角落库是锦上添花，绝不阻断调用方主流程。
    """
    cast = resolve_casting(store, project_id, script)
    existing = dict((store.get_video_style(project_id) or {}).get("voice_casting") or {})
    added: dict[str, str] = {}
    labels = dict((store.get_video_style(project_id) or {}).get("voice_labels") or {})
    for m in cast.members:
        if m.source == "casting" or not m.char_id:
            continue                      # 人工指定过：用户的表，一个字不动
        if m.char_id in existing:
            continue
        added[m.char_id] = m.voice_id
        if m.voice_label and m.voice_label != m.voice_id:
            labels[m.char_id] = m.voice_label
    if added:
        try:
            store.merge_video_style(project_id, {
                "voice_casting": {**existing, **added},
                "voice_labels": labels,
            })
        except Exception:  # noqa: BLE001
            log.warning("自动选角落库失败（忽略）：project=%s", project_id)
    return cast


def build_voice_pool(store: Any, visual_style: str | None = None) -> list[dict[str, Any]]:
    """下游用的完整音色池 = 勾选启用的发现音色（在前）+ 内置 8 个 + 克隆音色。

    为什么放这里而不是 api 层：配音链路（video/finalize）也要用同一份池，
    而 video 不该反向 import api。store 只是鸭子类型地调它的三个方法，
    casting 层本身仍不 import 任何 IO 模块。

    勾选的排前面：用户勾了哪些，自动选角就先用哪些——这是 BYOK 的本意。
    没勾选过时池=内置 8 个（按画风排序，漫剧年轻音色在前），与改造前完全一致；
    内置 8 个永远兜底，勾选只增不减。克隆音色厂商不给性别，归 other——
    只供手动指定给角色，不参与自动选角。
    """
    male, female = voice_pools(visual_style)
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in store.enabled_voice_pool():
        if r["voice_id"] in seen:
            continue
        seen.add(r["voice_id"])
        pool.append(r)
    for gender, p in (("male", male), ("female", female)):
        for vid in p:
            if vid in seen:
                continue
            seen.add(vid)
            pool.append({"voice_id": vid, "name": VOICE_LABELS.get(vid, vid), "gender": gender})
    for uv in store.list_user_voices():
        if uv.voice_id in seen:
            continue
        seen.add(uv.voice_id)
        pool.append({"voice_id": uv.voice_id, "name": uv.name or uv.voice_id, "gender": "other"})
    return pool


# ---- 兼容旧接口 --------------------------------------------------------

def assign_voices(
    script: Script,
    casting: dict[str, str] | None = None,
    *,
    visual_style: str | None = None,
) -> dict[str, str]:
    """旧接口：{角色名: 音色}。内部走 ``cast_script``，行为已修正。

    保留它是因为若干调用点按名字取音色；新代码建议直接用 ``cast_script``，
    因为它还带依据、告警和 id 键。
    """
    return cast_script(script, casting=casting, visual_style=visual_style).voices
