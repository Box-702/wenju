# =====================================================================
# generation.py —— 剧本生成流水线
#
# 把一个项目从「原始文本」生成「结构化剧本」。这里采用『线性』的多步
# 结构化生成（而非有状态的 Agent 图），以和后面的「改编 Agent」
# 形成明显区分：
#   - 生成：确定性编排的多阶段结构化抽取（故事圣经 -> 场景/节拍）；
#   - 改编：有状态、可工具调用、可人机协同的 LangGraph Agent。
#
# 无模型（未配置 key）或任一阶段失败时，回退为 make_source_script
# 产出的最小有效剧本，从而保证「导入 -> 生成」链路始终能跑通、可演示。
# =====================================================================

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import Settings
from ..domain import (
    Adaptation,
    Character,
    Location,
    Scene,
    Script,
    ScriptBeat,
    Source,
    normalize_id,
)
from ..llm import LLM
from .patch import make_source_script
from .profiles import profile_for, profile_type
from .chunking import chunk_text

log = logging.getLogger(__name__)


class _CharacterIn(BaseModel):
    """容错人物：id 可缺省（模型常只给 name），后续按序号补齐。"""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str = "未命名人物"
    role: str | None = None
    goal: str | None = None
    motivation: str | None = None
    description: str | None = None
    arc: str | None = None
    visual: str | None = None
    tags: str | None = None
    background: str | None = None
    history: str | None = None
    personality: str | None = None
    relationships: str | None = None


class _LocationIn(BaseModel):
    """容错地点：id 可缺省。"""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str = "未命名地点"
    description: str | None = None


class Bible(BaseModel):
    title: str = "未命名故事"
    logline: str = "主角面对核心冲突。"
    themes: list[str] = Field(default_factory=list)
    characters: list[_CharacterIn] = Field(default_factory=list)
    locations: list[_LocationIn] = Field(default_factory=list)


class _BeatIn(BaseModel):
    """容错节拍：兼容模型常用的 action/dialogue 键与 text/line 键。"""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    type: str = "action"
    text: str | None = None
    line: str | None = None
    action: str | None = None
    dialogue: str | None = None
    speaker: str | None = None
    emotion: str | None = None
    subtext: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _alias_keys(cls, data: object) -> object:
        # deepseek 实测会把动作节拍写成 {"type":"action","description":"..."}，
        # 不做别名映射的话节拍能通过校验但 text 全空（比报错更危险的静默退化）。
        if isinstance(data, dict) and not isinstance(data.get("text"), str) and not isinstance(data.get("line"), str):
            for key in ("description", "content", "summary"):
                if isinstance(data.get(key), str):
                    data = {**data, "text": data[key]}
                    break
        return data

    @field_validator("text", "line", "action", "dialogue", "subtext", mode="before")
    @classmethod
    def _unwrap_text(cls, v: object) -> object:
        # 模型偶尔把 dialogue 输出成 {"line": ...} / {"text": ...} 对象，解包成字符串。
        if isinstance(v, dict):
            for key in ("line", "text", "dialogue", "action", "value"):
                if isinstance(v.get(key), str):
                    return v[key]
            return None
        return v

    @field_validator("speaker", mode="before")
    @classmethod
    def _unwrap_speaker(cls, v: object) -> object:
        if isinstance(v, dict):
            for key in ("id", "name", "speaker"):
                if isinstance(v.get(key), str):
                    return v[key]
            return None
        return v


class _SceneIn(BaseModel):
    """容错场景：兼容 scene_id/scene_title/location 与 id/title/location_id。

    同时兼容模型两种常见输出形态：
      - beats：标准的节拍流数组；
      - action / dialogue：旧式兼容层（动作字符串数组 + 对白对象数组），
        当 beats 为空时由生成器合成节拍，避免整场戏空掉。
    """

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    scene_id: str | None = None
    title: str | None = None
    scene_title: str | None = None
    chapter_refs: list[str] = Field(default_factory=list)
    location_id: str | None = None
    location: str | None = None
    characters: list[str] = Field(default_factory=list)
    purpose: str = "推进情节。"
    conflict: str = "角色面对压力。"
    entry_state: str | None = None
    exit_state: str | None = None
    time: str | None = None
    time_of_day: str | None = None
    episode: int | None = None
    interior_exterior: str | None = None
    beats: list[_BeatIn] = Field(default_factory=list)

    @field_validator("episode", mode="before")
    @classmethod
    def _coerce_episode(cls, v: object) -> object:
        if v is None or isinstance(v, int):
            return v
        m = re.search(r"\d+", str(v))
        return int(m.group()) if m else None
    action: list[str] = Field(default_factory=list)
    dialogue: list[Any] = Field(default_factory=list)
    #: 段落改编决策（D-90）：本场覆盖的 seg id 与目标秒数。
    source_segments: list[str] = Field(default_factory=list)
    target_sec: float | None = None

    @field_validator("source_segments", mode="before")
    @classmethod
    def _coerce_source_segments(cls, v: object) -> object:
        # deepseek 偶尔把数组写成「seg_001、seg_002」的字符串。
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in re.split(r"[,，、\s]+", v) if x.strip()]
        return v

    @field_validator("action", mode="before")
    @classmethod
    def _coerce_action(cls, v: object) -> object:
        # deepseek 实测会把 action 输出成一个长字符串（分句用「；」），规整成数组。
        if v is None:
            return []
        if isinstance(v, str):
            return [seg.strip() for seg in re.split(r"[；;\n]", v) if seg.strip()]
        if isinstance(v, list):
            return [str(x) for x in v if isinstance(x, str) and x.strip()] or (
                [str(x).strip() for x in v if x] if v else []
            )
        return v

    @field_validator("dialogue", mode="before")
    @classmethod
    def _coerce_dialogue(cls, v: object) -> object:
        # 同实测形态：dialogue 输出成「角色（情绪）：『台词』」样式的单个字符串。
        if v is None:
            return []
        if isinstance(v, str):
            return cls._parse_dialogue_str(v)
        if isinstance(v, list):
            out: list[Any] = []
            for item in v:
                if isinstance(item, str):
                    out.extend(cls._parse_dialogue_str(item))
                else:
                    out.append(item)
            return out
        return v

    @staticmethod
    def _parse_dialogue_str(raw: str) -> list[dict[str, str]]:
        """「角色（情绪）：『台词』」→ 对白对象；没有说话人时只保留台词。"""
        text = raw.strip()
        if not text:
            return []
        m = re.match(r"^(.*?)(?:（(.*?)）)?[:：]\s*[「『\"']?(.+?)[」』\"']?$", text)
        if m and (m.group(3) or "").strip():
            entry: dict[str, str] = {"line": m.group(3).strip()}
            speaker = (m.group(1) or "").strip()
            if speaker:
                entry["speaker"] = speaker
            emotion = (m.group(2) or "").strip()
            if emotion:
                entry["emotion"] = emotion
            return [entry]
        return [{"line": text}]


def _synth_beats(scene: _SceneIn) -> list[_BeatIn]:
    """beats 为空时，用 action/dialogue 兼容层合成节拍流。"""
    beats: list[_BeatIn] = []
    for act in scene.action or []:
        if isinstance(act, str) and act.strip():
            beats.append(_BeatIn(type="action", text=act.strip()))
    for d in scene.dialogue or []:
        if isinstance(d, dict):
            line = str(d.get("line") or d.get("text") or "").strip()
            if line:
                beats.append(_BeatIn(type="dialogue", speaker=d.get("speaker"), line=line,
                                     emotion=d.get("emotion")))
        elif isinstance(d, str) and d.strip():
            beats.append(_BeatIn(type="dialogue", line=d.strip()))
    return beats


class ScenePlan(BaseModel):
    scenes: list[_SceneIn] = Field(default_factory=list)


def _is_degenerate_plan(plan: ScenePlan, raw_text: str) -> bool:
    """判断场景规划是否「结构退化」：素材够长，却只拆出极少场景。

    影视剧逻辑下，多章素材必须切成多场（同一地点 + 连续时间 + 一个戏剧目标 = 一场）。
    只拆出 1-2 场，说明模型把整段塞进了一场，下游分镜必然被迫退化成「一人一切」的硬切。
    """
    n_scenes = len(plan.scenes or [])
    n_chars = len(raw_text or "")
    if n_chars >= 3000:
        return n_scenes < 3
    if n_chars >= 1200:
        return n_scenes < 2
    return False


def _has_cjk(text: object) -> bool:
    """文本里是否含汉字（CJK 统一表意文字基本区）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _bible_naming_issue(bible: Bible, language: str) -> str | None:
    """故事圣经的「显示名」是否退化成了拼音/英文 slug。

    名字是下游一切（选角/人物字典/资产库引用）的地基，带病进场景阶段就全歪。
    判定只在中文输出下生效：改编英文原著时拉丁名是正当的。
    返回 None=没问题；返回字符串=一段可直接喂给重跑的修正指令。
    """
    if not str(language or "").lower().startswith("zh"):
        return None
    bad = [c.name for c in bible.characters if c.name and not _has_cjk(c.name)]
    bad += [loc.name for loc in bible.locations if loc.name and not _has_cjk(loc.name)]
    if not bad:
        return None
    return (
        "上一版把人物/地点的 name 写成了拼音或英文 slug（如 "
        + "、".join(repr(x) for x in bad[:5])
        + "）。name 必须是**原著中出现的中文称呼原文**（如「林然」「后巷便利店」）；"
        "拼音只能出现在 id 里。请重新输出完整 JSON。"
    )


def _bible_structure_issue(bible: Bible, language: str) -> str | None:
    """故事圣经是否**缺结构**（没有人物 / 没有地点）。

    缺人物 → 下游角色只能靠说话人反推，美术拿不到年龄身份，只能自己编外貌
    （2026-11-19 实测：38 岁的林远被写成「三十四岁」）；
    缺地点 → 所有场次落到兜底的 `loc_main「主要场景」`，导演/美术/摄影
    24 个镜头共用一个场景锚，卧室戏会带出办公工位（2026-11-19 实测《替身》）。

    成因：json_mode 不强制 schema，模型照系统提示词吐了另一套字段，
    characters/locations 整个数组缺失。提示词已给出 JSON 契约，这里是确定性
    护栏——别信模型自律（见 5.38/5.39 的规矩）。
    """
    problems: list[str] = []
    if not bible.characters:
        problems.append("characters 为空")
    if not bible.locations:
        problems.append("locations 为空")
    if not problems:
        return None
    return (
        "上一版故事圣经没有给出" + "、".join(problems) + "——下游会因此塌掉"
        "（地点会合并成「主要场景」，人物外貌只能靠编）。\n"
        "请严格按下面的 JSON 结构输出，且不要输出其它字段：\n"
        '{"title":"…","logline":"…","themes":["…"],'
        '"characters":[{"id":"char_001","name":"人物名","role":"protagonist|supporting|antagonist",'
        '"goal":"…","motivation":"…","description":"年龄、身份、性格、外貌线索"}],'
        '"locations":[{"id":"loc_001","name":"地点名","description":"环境、光线、道具"}]}\n'
        "characters 要列全原著的每一个人，locations 要列全原著的每一个地点，"
        "不要合并。原文中给出的人物设定原样写进 description。"
    )


def _bible_issue(bible: Bible, language: str) -> str | None:
    """故事圣经的可检出问题合成一段修正指令（None = 没问题）。"""
    parts = [
        p for p in (_bible_naming_issue(bible, language), _bible_structure_issue(bible, language))
        if p
    ]
    return "\n\n".join(parts) if parts else None


def _prior_block(prior_context: str) -> str:
    """前情提要注入块（连载选段改编时衔接上下文，禁止写进本次剧本）。"""
    text = (prior_context or "").strip()
    if not text:
        return ""
    return (
        f"\n\n【前情提要（此前已改编的部分，仅供保持人物与情节衔接，"
        f"严禁改编或复述进本次剧本）】\n{text[:800]}"
    )


def _stage_bible(llm: LLM, settings: Settings, *, title: str, excerpts: list[str], prof: dict[str, Any], language: str, prior_context: str = "", critique: str = "", bible_scope: str = "") -> Bible:
    """第一阶段：生成故事圣经（标题、梗概、主题、人物、地点）。"""
    from .profiles import profile_prompt

    text = "\n\n".join(excerpts[:6])
    prompt = (
        f"请基于下面的原著片段生成剧本故事圣经。标题：{title}\n"
        f"改编类型 profile：\n{profile_prompt(prof, language=language)}\n\n"
        "【输出结构（必须严格照此 JSON，不要输出任何其它字段）】\n"
        '{"title":"…","logline":"…","themes":["…"],'
        '"characters":[{"id":"char_001","name":"人物名","role":"protagonist|supporting|antagonist",'
        '"goal":"这个人物想要什么","motivation":"为什么",'
        '"description":"原文给出的全部设定：年龄、身份、性格、外貌线索",'
        '"visual":"视觉形象（年龄/外形/服装）","tags":"一句话核心标签",'
        '"background":"身份背景","history":"成长经历","personality":"性格特点",'
        '"relationships":"与其他人物的关系","arc":"人物弧线"}],'
        '"locations":[{"id":"loc_001","name":"地点名",'
        '"description":"这个地点的环境、光线、道具（按原文写，没有就写场景里能看见的东西）"}]}\n'
        "【命名硬规矩】人物与地点的 name 必须用原著中出现的称呼原文"
        "（中文原著就用中文名，如「林然」「后巷便利店」）；"
        "**严禁把 name 写成拼音或英文 slug**（如 lin_ran）——拼音只允许出现在 id 里。\n"
        "【必须列全的硬规矩】\n"
        "- characters 要覆盖原著出现的**每一个**人物（有名有姓的、只出场一句的都要列），"
        "role 要分清主角/反派/配角；\n"
        "- locations 要覆盖原著出现的**每一个**地点，逐个列，不要合并成「主要场景」；\n"
        "- 原文若已经给出人物设定（年龄、职业、性格），原样写进 description，"
        "不要改写、不要另编；\n"
        "- 原文若是剧本（带场次标题、人物设定、内景/外景），"
        "就按剧本的场次与地点来列，场次标题里的地点必须各自成条。\n\n"
        f"原著片段：\n{text[:4000]}"
        + _prior_block(prior_context)
    )
    if critique:
        prompt += f"\n\n上一版存在以下问题，请修正后重新输出：\n{critique}"
    if bible_scope:
        prompt += f"\n\n{bible_scope}"
    return llm.structured(Bible).invoke(
        [{"role": "system", "content": llm.system_prompt()}, {"role": "user", "content": prompt}]
    )


def _stage_scenes(llm: LLM, settings: Settings, *, bible: Bible, excerpts: list[str], prof: dict[str, Any], language: str, chunks: list[str], critique: str = "", prior_context: str = "", scope_note: str = "") -> ScenePlan:
    """第二阶段：生成场景与节拍流（动作 / 对白 / cue）。

    核心是「影视剧的场景划分逻辑」：一场戏 = 同一地点 + 连续时间 + 一个戏剧目标
    + 一场冲突 + 人物状态变化。严禁把整段原著塞进一场戏。

    ``scope_note``：故事规划（D-71）的事件底线 + 分集归属。给了就注入——
    事件清单是「一不许删」的硬约束，chapter_refs 要填所属集号（ep_00N），
    让 D-63 的集-镜两级结构与分集导出直接认账。
    """
    from .profiles import profile_prompt

    characters = [c.model_dump() for c in bible.characters]
    locations = [loc.model_dump() for loc in bible.locations]
    excerpt_block = "\n".join(excerpts[:8])[:4000]
    prompt = (
        "请把原著改编成「一场一场的戏」，并为每场生成按阅读顺序混排的 beats 节拍流。\n"
        f"改编类型 profile：\n{profile_prompt(prof, language=language)}\n\n"
        "【影视剧的场景划分逻辑，必须遵守】\n"
        "- 一场戏 = 同一地点 + 连续时间 + 一个清晰的戏剧目标 + 一场冲突 + 人物状态前后发生变化。\n"
        "- 按「戏剧单元」划分，不要把整段原著塞进一场；一场戏的容量约等于「一次连续的对峙 / 行动 / 发现」。\n"
        "- 原著中跨地点、跨时间的段落，必须切成不同的场。\n"
        "- 每场都要填写 purpose（本场要达成什么）、conflict（谁在阻碍什么）、"
        "entry_state / exit_state（人物进场/出场时的状态变化）。\n"
        "- 场景数量由素材量决定，不要为省事而合并：三章左右的素材通常应产出 5–10 场戏。\n"
        "- 动作节拍要写「看得见的画面」：谁、在什么位置、做了什么物理动作、周围有什么道具与光源。\n"
        "  不要只写「他说」「他想了想」这类拍不出来的内容——下游会直接把动作节拍变成视频提示词。\n"
        "- 对白节拍之间与前后，要有可见的动作或空间信息，方便转成画面。\n"
        "\n【分集剧本格式（硬要求）】\n"
        "- 每场填 episode（第几集，默认 1）、time（日/夜/晨/傍晚）、"
        "interior_exterior（内景/外景）；characters 必须列全动作和对白里出现的每一个人；\n"
        "- beats 的 type：action / dialogue / os（内心独白）/ flashback（闪回）/ "
        "sound（音效）/ subtitle（字幕）/ card（卡点或钩子）；\n"
        "- os 用 speaker+line（谁的内心的声音），flashback/sound/subtitle/card 用 text；\n"
        "- 每集最后一场留一个 card（结尾钩子），开场可用 card 标「开场卡点」；\n"
        "- 台词用口语，人物情绪写进 emotion；动作里禁止「心想/意识到/声音软下来」这类"
        "拍不出来的心理或听觉描写，改成可见的身体动作。\n"
    )
    if scope_note:
        prompt += f"\n{scope_note}\n"
    if critique:
        prompt += f"\n上一版存在以下问题，请修正后重新输出：\n{critique}\n"
    prompt += _prior_block(prior_context)
    prompt += (
        f"\n人物：{characters}\n地点：{locations}\n"
        f"故事圣经：{bible.model_dump_json()}\n\n"
        f"原著片段：\n{excerpt_block}"
    )
    return llm.structured(ScenePlan).invoke(
        [{"role": "system", "content": llm.system_prompt()}, {"role": "user", "content": prompt}]
    )


def _resolve_char(token: str, alias_to_id: dict[str, str]) -> str | None:
    """按别名索引认人物：名字（林远）/ id（char_001）/ 带尾号 id（char_001_1）。

    带尾号的处理：模型有时把 id 写成 ``char_001_1``（适配器补号留下的形状），
    去掉尾号仍能认回本人 —— 不会再凭空多出一个假角色。
    """
    t = (token or "").strip()
    if not t:
        return None
    hit = alias_to_id.get(t)
    if hit:
        return hit
    m = re.fullmatch(r"(.+?)_\d+", t)
    if m:
        return alias_to_id.get(m.group(1))
    return None


# 近义场景词 → 地点名字里的关键字。标题命中权重 2、正文命中 1：
# 「夜宴」在标题里比「道士」在动作里更能说明主场景。
# 家去掉了裸「家」「院」（现代对白/院墙会误伤），观保留「观内/堂内/蒲团」。
_LOCATION_CUES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("宴", "酒", "客", "席"), ("宴",)),
    (("妻", "堂屋", "家门", "家院"), ("家",)),
    (("观", "观内", "观中", "殿", "蒲团", "师父", "道士", "门人", "堂内"), ("观",)),
    (("登山", "山道", "山顶", "砍柴", "峰"), ("山",)),
)


def _best_full_name(
    title: str, text: str, alias_to_id: dict[str, str],
) -> str | None:
    """地点全名命中：名字越长越具体；同长取出现位置更靠后的（主场景更具体）。"""
    hits: list[tuple[int, int, str]] = []
    title = title or ""
    text = text or ""
    for name, lid in alias_to_id.items():
        if len(name) < 2 or name.startswith("loc_"):
            continue
        pos = -1
        if name in title:
            pos = 1000 + title.rfind(name)
        elif name in text:
            pos = text.rfind(name)
        if pos >= 0:
            hits.append((len(name), pos, lid))
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0][2]


def _location_scores(
    title: str, text: str, alias_to_id: dict[str, str],
) -> dict[str, int]:
    """给每个地点打近义场景词分：标题 2 / 正文 1。

    同一组里「观」和「观中」会同时命中，只算最具体的一条，否则重复计分
    （实测「观中」+「道士」+「堂内」被算成 4，和标题「宴」打平）。
    """
    scores: dict[str, int] = {}
    title = title or ""
    text = text or ""
    for name, lid in alias_to_id.items():
        if len(name) < 2 or name.startswith("loc_"):
            continue
        score = 0
        for cues, name_chars in _LOCATION_CUES:
            if not any(ch in name for ch in name_chars):
                continue
            matched = [cue for cue in cues if cue in title or cue in text]
            matched = [
                cue for cue in matched
                if not any(cue != other and cue in other for other in matched)
            ]
            for cue in matched:
                score += 2 if cue in title else 1
        if score:
            scores[lid] = scores.get(lid, 0) + score
    return scores


def resolve_location(
    raw: object, alias_to_id: dict[str, str], fallback: str,
    scene_text: str = "", scene_title: str = "",
) -> str:
    """把模型给的地点（id / 名字 / 场景文字线索）解析成 loc id。

    三级：模型显式 id/名字 → 标题/动作里的全名 → 近义场景词打分。
    2026-11-26 v3 实测：模型把「夜宴/辞行/传术」三场都填了 loc_003（王生家），
    但标题/动作里全是「观内/堂内/道士/蒲团」；所以非 fallback 的显式 id
    也会被高置信的场景文字覆盖（全名 10 分，或近义词 >=2 分且明显高于原地点）。
    """
    t = str(raw or "").strip()
    raw_id: str | None = None
    if t:
        raw_id = alias_to_id.get(t)
        if raw_id is None:
            raw_id = alias_to_id.get(normalize_id(t, "loc", fallback=""))
        if raw_id is None:
            for name, lid in alias_to_id.items():
                if len(name) >= 2 and (name in t or t in name):
                    raw_id = lid
                    break
    full = _best_full_name(scene_title, scene_text, alias_to_id)
    if full:
        # 父地点（劳山）vs 子场景（观宇）：动作里出现道观/殿/蒲团/门人时，
        # 取观宇，不把「劳山山顶道观门前」整场带成山道（v5 实测）。
        full_name = next(
            (n for n, lid in alias_to_id.items() if lid == full and not n.startswith("loc_")),
            "",
        )
        temple = next(
            (lid for n, lid in alias_to_id.items()
             if not n.startswith("loc_") and "观" in n),
            None,
        )
        # 父地点山、幻境月宫都不如「观宇」具体：动作里有道观/殿/蒲团/门人/
        # 道士时，取观宇；家（王生家）优先保留。
        if temple and "家" not in full_name and "观" not in full_name and any(
                k in (scene_text or "") for k in ("道观", "殿", "蒲团", "门人", "道士")):
            return temple
        if raw_id is None or raw_id == fallback or full != raw_id:
            return full
        return raw_id
    scores = _location_scores(scene_title, scene_text, alias_to_id)
    inferred, inferred_score = (None, 0)
    if scores:
        inferred = max(scores, key=scores.get)
        inferred_score = scores[inferred]
    raw_score = scores.get(raw_id, 0) if raw_id else 0
    if inferred:
        if raw_id is None or raw_id == fallback:
            return inferred
        if inferred != raw_id and inferred_score >= 2 and inferred_score > raw_score + 1:
            return inferred
    return raw_id or inferred or fallback


def infer_scene_time(text: str) -> str | None:
    """从场景动作里推断昼夜（场头 time 缺失时用）。"""
    blob = text or ""
    if any(k in blob for k in ("深夜", "夜里", "夜色", "月光", "月下", "夜")):
        return "夜"
    if any(k in blob for k in ("清晨", "凌晨", "拂晓", "晨")):
        return "晨"
    if any(k in blob for k in ("傍晚", "黄昏", "日暮", "薄暮")):
        return "傍晚"
    if any(k in blob for k in ("午后", "白天", "日间", "日")):
        return "日"
    return None


def infer_interior_exterior(title: str, location_name: str, text: str) -> str:
    """推断内景/外景（场头缺失时用）。默认内景。"""
    blob = f"{title or ''} {location_name or ''} {text or ''}"
    if "外景" in blob:
        return "外景"
    if "内景" in blob:
        return "内景"
    outdoor = ("山道", "山顶", "山路", "门外", "院门", "街道", "院外", "院中",
               "庭院", "院墙", "墙前", "田野", "河边", "崖", "林中")
    if any(k in blob for k in outdoor):
        return "外景"
    return "内景"


def _episode_from_refs(refs: list[str] | None) -> int:
    """chapter_refs 里的 ep_00N → 集号；没有就第 1 集。"""
    for ref in refs or []:
        m = re.search(r"ep_(\d+)", str(ref))
        if m:
            return max(1, int(m.group(1)))
    return 1


_TEMPLE_CUES = ("道士", "师父", "蒲团", "堂内", "观", "殿", "门人")


def inherit_previous_location(
    scene_loc: str, prev_loc: str | None, strong: bool, scene_text: str,
) -> str:
    """弱线索场景沿用上一场地点（同一连续空间里的辞行/传术）。

    2026-11-26 v3 实测：辞行/传术两场的动作只有「道士」，地点线索分数 1，
    模型又都填了王生家；前一场夜宴已经认到观宇，这两场应沿用观宇。
    只在没有全名/高分线索、且文本里出现道观人物/空间词时继承，避免把
    家里的戏也带过去。
    """
    if not strong and prev_loc and any(cue in (scene_text or "") for cue in _TEMPLE_CUES):
        return prev_loc
    return scene_loc


def _norm_beat_id(value: object, index: int, used: set[str]) -> str:
    """把模型给的节拍 id 规整为 ``beat_数字``（非纯数字则回退序号，并保证唯一）。"""
    raw = str(value or "").strip()
    candidate = normalize_id(raw, "beat", fallback=str(index))
    if not re.fullmatch(r"beat_[0-9]{3,}", candidate):
        candidate = f"beat_{index:03d}"
    n = index
    while candidate in used:
        n += 1
        candidate = f"beat_{n:03d}"
    used.add(candidate)
    return candidate


def generate_script(
    llm: LLM,
    settings: Settings,
    *,
    title: str,
    raw_text: str,
    adaptation_type: str,
    language: str,
    prior_context: str = "",
    story_plan: Any = None,
    dialogue_style: str = "",
    extra_scope: str = "",
    bible_scope: str = "",
    skip_adaptation: bool = False,
) -> tuple[Script, dict[str, Any]]:
    """生成剧本。返回 (Script, artifacts)。失败时回退最小剧本。

    prior_context：连载选段时的「前情提要」（已改编章节摘要），
    仅供模型保持人物与情节衔接，不进入本次剧本内容。

    story_plan（D-71 扩写流）：已确认的 StoryPlan。给了就按集扩写——
    事件底线注入场景阶段提示词，场次按提案归集（chapter_refs=ep_00N），
    D-63 的集-镜两级与分集导出直接认账。
    """
    prof = profile_for(adaptation_type)
    artifacts: dict[str, Any] = {"mode": "local-fallback"}

    scope_note = ""
    if story_plan is not None and getattr(story_plan, "status", "") == "confirmed":
        from .story_plan import episode_brief

        scope_note = "\n\n".join(
            episode_brief(story_plan, ep) for ep in story_plan.episodes
        ) + (
            "\n【归集】每场的 chapter_refs 填它承载的那一集（如 [\"ep_002\"]）；"
            "一场的节拍跨集时按主要冲突归集。"
        )
    # 按集扩写：一次只带本集任务书，由 story_plan.generate_from_plan 循环调用。
    if extra_scope:
        scope_note = (scope_note + "\n\n" + extra_scope).strip()

    # ---- 台词语言口径（文言原文必须由用户拍板白话/文言/混合）----
    from .source_style import (
        dialogue_style_note,
        is_classical_chinese,
        normalize_dialogue_style,
    )

    style = normalize_dialogue_style(dialogue_style)
    source_style = "classical" if is_classical_chinese(raw_text or "") else ""
    if style:
        scope_note = (scope_note + "\n\n" + dialogue_style_note(style)).strip()
        artifacts["dialogue_style"] = style
    elif source_style == "classical":
        artifacts["source_style"] = source_style
        log.info("原文是文言文；未指定台词语言（白话/文言/混合），沿用原文风格")

    # ---- 段落改编决策（D-90）----
    # 只在「短篇/无已确认故事提案」的路径跑：长篇的 D-71 事件清单已经是底线，
    # 再跑一次逐段分析会多花一次调用、两套口径还可能打架。
    adaptation_plan = None
    if llm.available and story_plan is None and not skip_adaptation:
        from .adaptation import (
            adaptation_summary,
            analyze_adaptation,
            is_narrative_source,
            plan_brief,
        )

        if is_narrative_source(raw_text or ""):
            adaptation_plan = analyze_adaptation(
                llm, title=title, raw_text=raw_text,
                adaptation_type=adaptation_type, language=language,
            )
            if adaptation_plan is not None:
                scope_note = (scope_note + "\n\n" + plan_brief(adaptation_plan)).strip()
                artifacts["adaptation_plan"] = adaptation_plan.model_dump()
                log.info("段落决策：%s", adaptation_summary(adaptation_plan))
            else:
                log.info("段落决策不可用（模型失败），走旧逻辑")

    if llm.available:
        try:
            chunks = chunk_text(raw_text or "", chunk_size=1200, overlap=160)
            if not chunks:
                chunks = [raw_text or ""]
            excerpts = chunks[:12]
            bible = _stage_bible(llm, settings, title=title, excerpts=excerpts, prof=prof, language=language, prior_context=prior_context, bible_scope=bible_scope)
            # 护栏：名层退化（拼音 slug）/ 结构缺失（没有人物、没有地点）时，
            # 带反馈重跑一次故事圣经（有界：只一次）。两种问题可能同时出现，
            # 合成一段 critique 一次问清，别让模型分两轮试错。
            critique = _bible_issue(bible, language)
            if critique:
                log.warning("故事圣经有问题，带反馈重跑一次：%s", critique[:80])
                retry = _stage_bible(
                    llm, settings, title=title, excerpts=excerpts, prof=prof,
                    language=language, prior_context=prior_context, critique=critique,
                    bible_scope=bible_scope,
                )
                if _bible_issue(retry, language) is None:
                    bible = retry
                else:
                    # 重跑仍不合格：不硬塞（带病进下游会一路错到底），落日志 + artifacts 记一笔，
                    # 由上游（对话层）如实告诉用户「模型这次没听话」。
                    log.warning("故事圣经重跑后仍有问题，保留但下游可能沿用兜底值")
            plan = _stage_scenes(llm, settings, bible=bible, excerpts=excerpts, prof=prof, language=language, chunks=chunks, prior_context=prior_context, scope_note=scope_note)
            # 退化护栏：把多章素材挤进过少的场景（结构退化）时，带反馈重拆一次。
            if _is_degenerate_plan(plan, raw_text):
                log.warning("场景规划退化（%d 场 / %d 字），带反馈重拆", len(plan.scenes), len(raw_text))
                plan = _stage_scenes(
                    llm, settings, bible=bible, excerpts=excerpts, prof=prof, language=language, chunks=chunks,
                    critique=(
                        "上一版把多章素材挤进了过少的场景里（例如整段只拆出一场，或只拆出 2 场）。"
                        "请严格按「同一地点 + 连续时间 + 一个戏剧目标 + 一场冲突」重新切分，"
                        "跨地点、跨时间的段落必须切成不同的场。"
                    ),
                    prior_context=prior_context,
                    scope_note=scope_note,
                )
            # 段落决策一致性：段落没落场、cut 段混进来、目标秒数偏差过大时，
            # 带反馈重拆一次（有界：只一次；仍不合格就交给下面的绑定做兜底修复）。
            if adaptation_plan is not None and plan.scenes:
                from .adaptation import scene_plan_issues

                issues = [x for x in scene_plan_issues(adaptation_plan, plan)
                          if x.startswith("错误")]
                if issues:
                    log.warning("场景规划与段落决策不一致，带反馈重拆一次：%s",
                                "；".join(issues)[:160])
                    retry = _stage_scenes(
                        llm, settings, bible=bible, excerpts=excerpts, prof=prof,
                        language=language, chunks=chunks, prior_context=prior_context,
                        scope_note=scope_note, critique="\n".join(issues),
                    )
                    if retry.scenes:
                        plan = retry
            if not plan.scenes:
                # 思考模型输出不稳定：空场景带反馈重试一次（只说「重试」它还会空）。
                plan = _stage_scenes(
                    llm, settings, bible=bible, excerpts=excerpts, prof=prof,
                    language=language, chunks=chunks, prior_context=prior_context,
                    scope_note=scope_note,
                    critique=(
                        "上一版 scenes 为空数组。请只返回 scenes 数组：每场 2–5 个节拍、"
                        "每场填 episode/time/interior_exterior/characters，不要输出空数组"
                        "或其它顶层结构。"
                    ),
                )
            if not plan.scenes:
                raise ValueError("场景生成为空")

            # ---- 人物：Bible 中若没有，从节拍说话人自动补全 ----
            used_chars: set[str] = set()
            chars: list[Character] = []
            # 别名索引：**名字**与 **id** 都算别名。模型在场景规划里可能用
            # id 指代人物（"char_001"）而不是名字（"林远"）。只按名字匹配 →
            # 认不出 → 被当成「原著里没出现过的新人」→ 凭空多出一批假角色
            # （char_001_1，名字就叫 "char_001"）。2026-11-20《替身v2》实测：
            # 7 个角色被撑成 12 个，其中 5 个是这种假人——下游会照着它们多生
            # 5 张定妆照（真金白银），视频提示词里还会冒出叫 char_001 的「角色」。
            alias_to_id: dict[str, str] = {}
            for i, c in enumerate(bible.characters):
                cid = normalize_id(c.id, "char", fallback=f"char_{i + 1:03d}")
                if cid in used_chars:
                    cid = f"{cid}_{i + 1}"[: 40]
                used_chars.add(cid)
                chars.append(Character(id=cid, name=c.name, role=c.role, goal=c.goal,
                                       motivation=c.motivation,
                                       # 人物设定（年龄/身份/性格）必须带到下游：
                                       # 美术阶段只读人物描述来写角色造型，
                                       # 丢在这里就等于让美术自己编外貌。
                                       description=c.description, arc=c.arc,
                                       visual=c.visual, tags=c.tags,
                                       background=c.background, history=c.history,
                                       personality=c.personality,
                                       relationships=c.relationships))
                for key in (c.name, c.id):
                    k = str(key or "").strip()
                    if k and k not in alias_to_id:
                        alias_to_id[k] = cid
            extra_speakers: list[str] = []
            for sc in plan.scenes:
                for b in sc.beats or []:
                    spk = str(b.speaker or "").strip()
                    if spk and not _resolve_char(spk, alias_to_id) and spk not in extra_speakers:
                        extra_speakers.append(spk)
            for spk in extra_speakers:
                cid = normalize_id(spk, "char", fallback=f"char_{len(chars) + 1:03d}")
                # 保证与已有角色 id 不冲突（模型常把同一人写成不同名字）。
                if cid in used_chars:
                    suffix = 1
                    while f"{cid}_{suffix}" in used_chars:
                        suffix += 1
                    cid = f"{cid}_{suffix}"
                used_chars.add(cid)
                chars.append(Character(id=cid, name=spk, role="supporting"))
                alias_to_id[spk] = cid
            if not chars:
                chars = [Character(id="char_protagonist", name="主角", role="protagonist")]

            # ---- 地点 ----
            used_locs: set[str] = set()
            locs: list[Location] = []
            for i, loc in enumerate(bible.locations):
                lid = normalize_id(loc.id, "loc", fallback=f"loc_{i + 1:03d}")
                if lid in used_locs:
                    lid = f"{lid}_{i + 1}"[: 40]
                used_locs.add(lid)
                locs.append(Location(id=lid, name=loc.name, description=loc.description))
            if not locs:
                locs = [Location(id="loc_main", name="主要场景")]

            # 地点别名索引：名字与 id 都认，供 resolve_location 把模型给的中文
            # 地名/带修饰的地名解析回 loc id（否则全部回退到第一个地点）。
            loc_alias_to_id: dict[str, str] = {}
            for loc in locs:
                for key in (loc.name, loc.id):
                    k = str(key or "").strip()
                    if k and k not in loc_alias_to_id:
                        loc_alias_to_id[k] = loc.id

            char_id_set = {c.id for c in chars}
            char_list = [c.id for c in chars]
            loc_id = locs[0].id
            scenes: list[Scene] = []
            used_scene_ids: set[str] = set()
            prev_scene_loc: str | None = None
            for i, s in enumerate(plan.scenes or []):
                sid = normalize_id(s.scene_id or s.id, "scene", fallback=f"scene_{i + 1:03d}")
                # 模型常给出带后缀的场景 id（如 scene_01_wake_up），而 Scene.id
                # 规定纯数字（scene_NNN）：不合规时回退序号，并保证唯一。
                if not re.fullmatch(r"scene_[0-9]{3,}", sid):
                    sid = f"scene_{i + 1:03d}"
                n = i + 1
                while sid in used_scene_ids:
                    n += 1
                    sid = f"scene_{n:03d}"
                used_scene_ids.add(sid)
                s_title = s.scene_title or s.title
                s_loc = s.location_id or s.location
                # 场景人物：显式人物（名字/ id 均可）+ 该场景节拍的说话人。
                sc_chars = [_resolve_char(str(x).strip(), alias_to_id)
                            or normalize_id(x, "char", fallback=x) for x in s.characters]
                sc_chars = [c for c in sc_chars if c in char_id_set]
                beats: list[ScriptBeat] = []
                used_beat_ids: set[str] = set()
                # beats 为空时用 action/dialogue 兼容层合成，避免整场戏没有节拍。
                raw_beats = s.beats or _synth_beats(s)
                _beat_aliases = {
                    "os": "os", "内心": "os", "内心os": "os", "monologue": "os",
                    "flashback": "flashback", "闪回": "flashback",
                    "sound": "sound", "音效": "sound",
                    "subtitle": "subtitle", "字幕": "subtitle",
                    "card": "card", "hook": "card", "卡点": "card", "钩子": "card",
                }
                for bi, b in enumerate(raw_beats):
                    raw_kind = str(b.type or "action").strip().lower()
                    kind = _beat_aliases.get(raw_kind, raw_kind)
                    if kind not in {"action", "dialogue", "cue", "os",
                                    "flashback", "sound", "subtitle", "card"}:
                        kind = "dialogue" if (b.speaker or b.line or b.dialogue) else "action"
                    beat_text = str(b.text or b.action or b.line or "").strip()
                    # 模型常把「三天前/从此」也标 card；前者是闪回、后者是过渡旁白，
                    # 不是钩子。按内容纠正，别让 UI 全打成【卡点】。
                    if kind == "card" and beat_text:
                        if any(k in beat_text for k in
                               ("三天前", "三年前", "几年前", "此前", "原来", "当初", "那一年")):
                            kind = "flashback"
                        elif any(k in beat_text for k in
                                 ("从此", "后来", "就这样", "于是", "接下来")):
                            kind = "subtitle"
                    bid = _norm_beat_id(b.id, bi + 1, used_beat_ids)
                    if kind in ("dialogue", "os"):
                        speaker = _resolve_char(str(b.speaker or "").strip(), alias_to_id) or (
                            sc_chars[0] if sc_chars else char_list[0])
                        line = str(b.line or b.dialogue or b.text or "").strip()
                        if not line:
                            continue
                        beats.append(ScriptBeat(
                            id=bid, type=kind, speaker=speaker, line=line,
                            emotion=str(b.emotion or "").strip() or None,
                        ))
                    else:
                        text = str(b.text or b.action or b.line or "").strip()
                        if not text:
                            continue
                        beats.append(ScriptBeat(id=bid, type=kind, text=text))  # type: ignore[arg-type]
                # 动作里提到但 characters 没列的人物补上（选角/锚点会漏）。
                # 只扫动作/画面节拍：对白里「老道士骗我」不代表道士在场。
                corpus = " ".join(
                    [str(x) for x in (s.action or [])]
                    + [(b.text or "") for b in (s.beats or [])
                       if str(getattr(b, "type", "action") or "action").strip().lower()
                       in ("action", "cue")]
                )
                for c in chars:
                    if c.name and c.name in corpus and c.id not in sc_chars:
                        sc_chars.append(c.id)
                sc_chars = sc_chars or [char_list[0]]
                # 地点推断只看标题/目的/动作，**不看对白**——「我在家哪受过这种苦」
                # 会把观宇的戏带成王生家（2026-11-26 v3 实测）。
                # 地点推断只看动作/画面节拍；card（「三天前从劳山归来」）和
                # 闪回/音效/字幕是时间跳转或声音，不是当前空间（v5 实测）。
                scene_text = " ".join([
                    s.purpose or "", s.conflict or "",
                    " ".join(str(x) for x in (s.action or [])),
                    " ".join((b.text or "") for b in (s.beats or [])
                             if str(getattr(b, "type", "action") or "action").strip().lower()
                             in ("action", "cue")),
                ])
                full_name = _best_full_name(s_title or "", scene_text, loc_alias_to_id)
                cue_scores = _location_scores(s_title or "", scene_text, loc_alias_to_id)
                strong = bool(full_name) or max(cue_scores.values(), default=0) >= 2
                scene_loc = resolve_location(
                    s_loc, loc_alias_to_id, loc_id, scene_text, s_title or "")
                scene_loc = inherit_previous_location(
                    scene_loc, prev_scene_loc, strong, scene_text)
                prev_scene_loc = scene_loc
                loc_name = next((l.name for l in locs if l.id == scene_loc), "")
                raw_ie = str(s.interior_exterior or "").strip()
                if raw_ie in ("内景", "内", "INT", "int"):
                    int_ext = "内景"
                elif raw_ie in ("外景", "外", "EXT", "ext"):
                    int_ext = "外景"
                else:
                    int_ext = infer_interior_exterior(s_title or "", loc_name, scene_text)
                time_value = s.time or s.time_of_day or infer_scene_time(scene_text)
                scene_episode = s.episode or _episode_from_refs(s.chapter_refs)
                scenes.append(
                    Scene(
                        id=sid,
                        title=s_title or f"第 {i + 1} 场",
                        chapter_refs=s.chapter_refs or ["ch_001"],
                        location_id=scene_loc,
                        time=time_value,
                        episode=scene_episode,
                        interior_exterior=int_ext,
                        characters=sc_chars,
                        purpose=s.purpose,
                        conflict=s.conflict,
                        entry_state=s.entry_state,
                        exit_state=s.exit_state,
                        action=[b.text for b in beats if b.type == "action" and b.text],
                        # 兼容字段 dialogue 必须与 beats 同步：留空会让后续
                        # 「只改 action」的 patch 重建节拍时把对白整体冲掉。
                        dialogue=[
                            {"speaker": b.speaker, "line": b.line}
                            for b in beats
                            if b.type == "dialogue" and b.speaker and b.line
                        ],
                        beats=beats,
                    )
                )
            if not scenes:
                raise ValueError("场景生成为空")

            # 剧本型原文：场次标题与昼夜按原文回填（模型没写才补，见 screenplay.py）
            from .screenplay import backfill_scenes_from_source

            loc_names = {loc.id: loc.name for loc in locs}
            n_title, n_time = backfill_scenes_from_source(scenes, loc_names, raw_text or "")
            if n_title or n_time:
                log.info("按原文回填：场次标题 %d 场、昼夜 %d 场", n_title, n_time)
            # 非剧本型原文没有场次抬头，推断不出就落到「日」，别留空场头。
            for sc in scenes:
                if not (sc.time or "").strip():
                    sc.time = "日"

            if adaptation_plan is not None:
                from .adaptation import bind_scene_targets

                bind_notes = bind_scene_targets(adaptation_plan, plan, scenes)
                for note in bind_notes:
                    log.info("段落绑定：%s", note)
                artifacts["adaptation_plan"] = adaptation_plan.model_dump()

            script = Script(
                title=bible.title or title,
                version="1.0",
                language=language,
                adaptation=Adaptation(type=profile_type(adaptation_type), target_format=prof["target_format"], tone=prof["tone"]),
                source=Source(chapter_count=len(chunks), chapter_ids=[f"ch_{i + 1:03d}" for i in range(len(chunks))]),
                logline=bible.logline,
                themes=bible.themes,
                characters=chars,
                locations=locs,
                scenes=scenes,
                adaptation_plan=adaptation_plan,
                dialogue_style=style,
                genre=prof.get("label"),
                synopsis="\n".join(sc.purpose or "" for sc in scenes if sc.purpose) or None,
            )
            adaptation_issues: list[str] = []
            from .script_text import audit_script_text, ensure_hook_card

            cards_added = ensure_hook_card(script, adaptation_plan)
            artifacts["hook_cards"] = cards_added
            text_issues = audit_script_text(script)
            rewritten = 0
            if style == "vernacular":
                from .script_rewrite import rewrite_vernacular

                script, rewritten = rewrite_vernacular(llm, script)
                artifacts["text_rewrite"] = rewritten
                if rewritten:
                    text_issues = audit_script_text(script)
            artifacts["text_audit"] = text_issues
            for issue in text_issues:
                log.warning("剧本文本：%s", issue)

            if adaptation_plan is not None:
                from .adaptation import coverage_issues

                adaptation_issues = coverage_issues(script, adaptation_plan)
                artifacts["adaptation_issues"] = adaptation_issues
                for issue in adaptation_issues:
                    log.warning("段落改编体检：%s", issue)
            artifacts = {
                "mode": "llm",
                "bible": bible.model_dump(),
                "scene_count": len(scenes),
                "naming_degraded": _bible_naming_issue(bible, language) is not None,
                "source_backfill": {"titles": n_title, "times": n_time},
                "story_plan": (
                    {"episodes": len(story_plan.episodes),
                     "events": len(story_plan.understanding.events)}
                    if story_plan is not None else None
                ),
                "adaptation_plan": (
                    adaptation_plan.model_dump() if adaptation_plan is not None else None
                ),
                "adaptation_issues": adaptation_issues if adaptation_plan is not None else [],
                "dialogue_style": style,
                "source_style": source_style,
                "text_audit": text_issues,
                "text_rewrite": rewritten,
                "hook_cards": cards_added,
            }
            return script, artifacts
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 生成失败，回退本地规则剧本：%s", e)
            # 文言原文 + 已确认白话时，本地回退会把文言原文整段塞进 action 节拍
            # （v6 实测），那比失败更糟：宁可显式失败，让上游重试/报错。
            if source_style == "classical" and style:
                raise
            artifacts = {"mode": "local-fallback", "error": str(e)}

    script = make_source_script(title, raw_text, adaptation_type=adaptation_type, language=language)
    return script, artifacts
