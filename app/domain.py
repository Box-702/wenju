# =====================================================================
# domain.py —— 剧本领域模型（数据契约）
#
# 这是「问剧」主题的核心数据结构。它刻画一部结构化剧本：
#   剧本 Script -> 人物 Character / 地点 Location / 场景 Scene
#   场景 Scene   -> 节拍流 beats（动作 / 对白 / cue）+ 兼容字段 action / dialogue
#
# 模型用 Pydantic v2 定义，并带 `mode="before"` 校验器，把 LLM 输出的
# 自由文本（role / fidelity / id）规整到受约束的枚举与稳定 id 上，
# 从而让下游的 Agent patch、diff、校验和导出都能依赖稳定标识。
#
# 这一层刻意只放「数据形状与规整逻辑」，不碰存储、不碰 LLM、不碰 LangGraph，
# 保证它能在测试里被单独、稳定地验证。
# =====================================================================

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---- 受约束的枚举 -----
AdaptationType = Literal["series", "film", "short_drama", "stage", "other"]
RoleType = Literal["protagonist", "antagonist", "supporting", "mentor", "foil", "other"]
Fidelity = Literal["faithful", "compressed", "reordered", "merged", "invented"]
Gender = Literal["male", "female", "unknown"]

# 画风：与改编类型正交的一个维度。
#
# 为什么把「漫剧」放在这里而不是 AdaptationType 里：短剧是按载体/时长/分发
# 定义的（竖屏、1–2 分钟、前 3 秒抓人），漫剧是按画风定义的（二次元、分格、
# 符号化表演）。两者不在同一根轴上——漫剧就是「短剧 + 二次元画风」，是短剧
# 的子类，不是并列体裁。做成并列类型会让导演、节奏、校验层为一个画风分叉，
# 那是纯成本。画风只该影响三件事：美术锚与定妆图 prompt、视频提示词的画风
# 声明、配音气质。
VisualStyle = Literal["realistic", "anime", "3d", "ink"]

# ---- id / 枚举规整 ----
_ID_SUFFIX_RE = re.compile(r"[^a-z0-9_]+")


def _slug_token(value: str) -> str:
    """把任意字符串转成小写、适合 snake_case 的 token。"""
    s = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    s = _ID_SUFFIX_RE.sub("", s)
    return re.sub(r"_+", "_", s).strip("_")


def _strip_prefix(seed: str, prefix: str) -> str:
    """剥掉字符串开头已有的 `<prefix>_` 前缀（大小写不敏感）。"""
    low = seed.lower()
    if low.startswith(prefix.lower() + "_"):
        return seed[len(prefix) + 1 :]
    if low.startswith(prefix.lower()):
        return seed[len(prefix) :]
    return seed


def assign_dialogue_ranges(shots: list, beats: list) -> dict[str, list]:
    """按镜头的 ``dialogue_ref`` 把台词切给各镜。

    规则：一镜的台词 = 从它引用的那个 beat 开始，往后取**同一个说话人**连续说的
    几句，碰到换人（或下一个镜头的分界点）就停。这样一段「甲乙来回说话」的连续
    对话可以按说话人拆成多镜，每镜只拿自己那几句。

    为什么要限制「同一说话人」：否则一个镜头认领一句之后，会把后面所有人的台词
    一路吞到下一个分界点，配音时全从这一个人嘴里出来。

    为什么不按「整段对话」分配：那会把两个人的台词都算进每一镜——断言会误报
    「一镜多说话人」，配音也会把台词塞到画面里另一个人身上（2026-10-31 实测：
    观众完全不知道是谁在说）。返回 ``{shot_id: [beat, ...]}``，没有台词的镜头不出现。
    """
    lines = [b for b in (beats or []) if b.type == "dialogue" and (b.line or "").strip()]
    pos = {b.id: i for i, b in enumerate(lines)}
    cuts: list[tuple[int, object]] = []
    seen: set[int] = set()
    for s in sorted(shots, key=lambda x: x.order):
        ref = getattr(s, "dialogue_ref", None)
        # 两个镜头引用同一句：只有第一个算数，后一个的范围留给它自己后面的话。
        if ref in pos and pos[ref] not in seen:
            seen.add(pos[ref])
            cuts.append((pos[ref], s))
    cuts.sort(key=lambda x: x[0])

    ranges: dict[str, list] = {}
    for k, (start, shot) in enumerate(cuts):
        nxt = cuts[k + 1][0] if k + 1 < len(cuts) else len(lines)
        speaker = lines[start].speaker
        end = start + 1
        while end < nxt and lines[end].speaker == speaker:
            end += 1
        ranges[shot.id] = lines[start:end]
    return ranges


def normalize_id(value: object, prefix: str, *, fallback: str | None = None) -> str:
    """把 LLM 给的 id 规整为 `<prefix>_[a-z0-9_]+`。

    策略：保留已合格 id；去掉已有 prefix 再重加；无剩余内容时用 fallback 或 hash。
    对 scene / beat 这类数字 id，不足三位自动补零，保证稳定可寻址。

    注意：fallback 也要先剥前缀，否则 fallback 传进来的 "loc_main" 会被再拼一次，
    变成 "loc_loc_main"，导致引用对不上。
    """
    seed = str(value).strip() if value is not None else (fallback or "")
    slug = _slug_token(_strip_prefix(seed, prefix))
    if not slug:
        slug = _slug_token(_strip_prefix(str(fallback or "").strip(), prefix))
    if not slug:
        slug = hashlib.md5(str(value).encode("utf-8")).hexdigest()[:8]
    if prefix in {"scene", "beat"} and slug.isdigit():
        slug = slug.zfill(3)
    return f"{prefix}_{slug}"


_ROLE_ALIASES: dict[str, RoleType] = {
    "protagonist": "protagonist", "hero": "protagonist", "主角": "protagonist",
    "主人公": "protagonist", "第一主角": "protagonist",
    "antagonist": "antagonist", "villain": "antagonist", "反派": "antagonist",
    "对手": "antagonist", "敌人": "antagonist", "boss": "antagonist",
    "mentor": "mentor", "teacher": "mentor", "导师": "mentor", "老师": "mentor",
    "foil": "foil", "对照": "foil", "反衬": "foil",
    "supporting": "supporting", "sidekick": "supporting", "配角": "supporting",
    "辅助": "supporting", "助手": "supporting", "npc": "supporting",
    "other": "other", "narrator": "other", "旁白": "other",
}


def normalize_role(value: object) -> RoleType | None:
    """把自由文本角色名映射到 RoleType；无法识别时回退为 ``other``。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if text in _ROLE_ALIASES:
        return _ROLE_ALIASES[text]
    key = re.sub(r"[\s_]+", "", text).lower()
    if key in _ROLE_ALIASES:
        return _ROLE_ALIASES[key]
    for suffix in ("角色", "role"):
        if key.endswith(suffix) and key[: -len(suffix)] in _ROLE_ALIASES:
            return _ROLE_ALIASES[key[: -len(suffix)]]
    candidates = sorted(_ROLE_ALIASES.keys(), key=len, reverse=True)
    for alias in candidates:
        if alias and alias in key:
            return _ROLE_ALIASES[alias]
    return "other"


_FIDELITY_ALIASES: dict[str, Fidelity] = {
    "faithful": "faithful", "忠实": "faithful", "原貌": "faithful",
    "compressed": "compressed", "压缩": "compressed", "精简": "compressed",
    "reordered": "reordered", "重排": "reordered",
    "merged": "merged", "合并": "merged",
    "invented": "invented", "新增": "invented", "原创": "invented",
}


def normalize_fidelity(value: object) -> Fidelity | None:
    """把自由文本 fidelity 映射到枚举；无法识别时回退为 ``compressed``。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    if text in _FIDELITY_ALIASES:
        return _FIDELITY_ALIASES[text]
    key = re.sub(r"[\s_]+", "", text).lower()
    if key in _FIDELITY_ALIASES:
        return _FIDELITY_ALIASES[key]
    for alias in sorted(_FIDELITY_ALIASES.keys(), key=len, reverse=True):
        if alias and alias in key:
            return _FIDELITY_ALIASES[alias]
    return "compressed"


_ADAPTATION_ALIASES: dict[str, AdaptationType] = {
    "series": "series", "tv": "series", "drama": "series", "剧集": "series",
    "电视剧": "series", "连续剧": "series",
    "film": "film", "movie": "film", "电影": "film", "院线": "film",
    "short_drama": "short_drama", "short": "short_drama", "短剧": "short_drama",
    "stage": "stage", "theatre": "stage", "舞台": "stage", "舞台剧": "stage",
    "other": "other", "其他": "other",
}


def normalize_gender(value: object) -> Gender:
    """把各种写法（male / 男 / 男性 …）归一到性别枚举；无法识别回退 unknown。

    为什么不靠推断填这个字段：性别是「人物设定」的一部分，应该由剧本阶段
    直接标注或人工确认；推断（名字用字、代词）只是拿不到时的兜底，见
    ``app/media/casting.py``。
    """
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    if text in {"male", "m", "man", "男", "男性", "男声", "男角"}:
        return "male"
    if text in {"female", "f", "woman", "女", "女性", "女声", "女角"}:
        return "female"
    if "female" in text or "女" in text:
        return "female"
    if "male" in text or "男" in text:
        return "male"
    return "unknown"


def normalize_adaptation_type(value: object) -> AdaptationType:
    """把自由文本改编类型映射到枚举；无法识别时回退为 ``other``。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        return "other"
    if text in _ADAPTATION_ALIASES:
        return _ADAPTATION_ALIASES[text]
    key = re.sub(r"[\s_]+", "", text).lower()
    if key in _ADAPTATION_ALIASES:
        return _ADAPTATION_ALIASES[key]
    for alias in sorted(_ADAPTATION_ALIASES.keys(), key=len, reverse=True):
        if alias and alias in key:
            return _ADAPTATION_ALIASES[alias]
    return "other"


def normalize_visual_style(value: object) -> VisualStyle:
    """把画风的各种写法归一到枚举；无法识别回退 ``realistic``（实拍）。

    为什么兜底是实拍而不是二次元：默认画风决定整片观感，猜错成二次元
    比猜错成实拍更突兀，且实拍是当前视频模型最稳的产出。
    """
    text = str(value or "").strip().lower()
    if text in {"realistic", "live", "live_action", "写实", "实拍", "真人"}:
        return "realistic"
    if text in {"anime", "2d", "comic", "漫剧", "二次元", "动画", "动态漫"}:
        return "anime"
    if text in {"3d", "cgi", "三维"}:
        return "3d"
    if text in {"ink", "水墨"}:
        return "ink"
    if "anime" in text or "漫" in text or "二次元" in text or "动画" in text:
        return "anime"
    if "3d" in text or "三维" in text:
        return "3d"
    if "水墨" in text or "ink" in text:
        return "ink"
    return "realistic"


# ---- 领域模型 -----


class Character(BaseModel):
    """人物。id 需为 ``char_xxx`` 且全剧唯一。"""

    id: str = Field(pattern=r"^char_[a-z0-9_]+$")
    name: str
    role: RoleType | None = None
    gender: Gender = "unknown"
    goal: str | None = None
    motivation: str | None = None
    personality: str | None = None
    arc: str | None = None
    speech_style: str | None = None

    @field_validator("gender", mode="before")
    @classmethod
    def _coerce_gender(cls, v: object) -> object:
        return normalize_gender(v)

    @field_validator("role", mode="before")
    @classmethod
    def _coerce_role(cls, v: object) -> object:
        return normalize_role(v)

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> object:
        return normalize_id(v, "char", fallback=v)


class Location(BaseModel):
    """地点。id 需为 ``loc_xxx``。"""

    id: str = Field(pattern=r"^loc_[a-z0-9_]+$")
    name: str
    description: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> object:
        return normalize_id(v, "loc", fallback=v)


class DialogueLine(BaseModel):
    """兼容结构里的对白行。speaker 必须引用某个 ``char_xxx``。"""

    speaker: str = Field(pattern=r"^char_[a-z0-9_]+$")
    line: str
    emotion: str | None = None
    subtext: str | None = None

    @field_validator("speaker", mode="before")
    @classmethod
    def _coerce_speaker(cls, v: object) -> object:
        return normalize_id(v, "char", fallback=v)


class ScriptBeat(BaseModel):
    """剧本流节拍：按阅读顺序混排动作 / 对白 / cue。id 需为 ``beat_数字``。"""

    id: str = Field(pattern=r"^beat_[0-9]{3,}$")
    type: Literal["action", "dialogue", "cue"]
    text: str | None = None
    speaker: str | None = Field(default=None, pattern=r"^char_[a-z0-9_]+$")
    line: str | None = None
    emotion: str | None = None
    subtext: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> object:
        return normalize_id(v, "beat", fallback=v)

    @field_validator("speaker", mode="before")
    @classmethod
    def _coerce_speaker(cls, v: object) -> object:
        if v is None or str(v).strip() == "":
            return None
        return normalize_id(v, "char", fallback=v)


class AdaptationNotes(BaseModel):
    """某个场景的改编说明。"""

    reason: str | None = None
    fidelity: Fidelity | None = None

    @field_validator("fidelity", mode="before")
    @classmethod
    def _coerce_fidelity(cls, v: object) -> object:
        return normalize_fidelity(v)


class Scene(BaseModel):
    """场景。id 需为 ``scene_数字``。

    ``beats`` 是主编辑结构；``action`` / ``dialogue`` 是兼容层，
    由后端在应用 patch 后从 beats 自动同步。
    """

    id: str = Field(pattern=r"^scene_[0-9]{3,}$")
    title: str
    chapter_refs: list[str] = Field(min_length=1)
    location_id: str = Field(pattern=r"^loc_[a-z0-9_]+$")
    time: str | None = None
    characters: list[str] = Field(min_length=1)
    purpose: str
    conflict: str
    entry_state: str | None = None
    exit_state: str | None = None
    action: list[str] = Field(default_factory=list)
    dialogue: list[DialogueLine] = Field(default_factory=list)
    beats: list[ScriptBeat] = Field(default_factory=list)
    adaptation_notes: AdaptationNotes | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> object:
        return normalize_id(v, "scene", fallback=v)

    @field_validator("location_id", mode="before")
    @classmethod
    def _coerce_location_id(cls, v: object) -> object:
        return normalize_id(v, "loc", fallback=v)

    @field_validator("characters", mode="before")
    @classmethod
    def _coerce_characters(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        return [normalize_id(x, "char", fallback=x) for x in v]


class Source(BaseModel):
    """来源信息：原始章节数与其 id（至少 3 章才符合产品预期）。"""

    chapter_count: int = Field(ge=1)
    chapter_ids: list[str] = Field(min_length=1)


class Adaptation(BaseModel):
    """改编元信息：类型与目标格式。"""

    type: AdaptationType
    target_format: str | None = None
    tone: str | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, v: object) -> object:
        return normalize_adaptation_type(v)


class Script(BaseModel):
    """顶层剧本对象，对应 schema/script.schema.json。"""

    model_config = ConfigDict(validate_assignment=False)

    title: str
    version: str = Field(default="1.0")
    language: str = "zh-CN"
    adaptation: Adaptation | None = None
    source: Source
    logline: str = Field(min_length=1)
    themes: list[str] = Field(default_factory=list)
    characters: list[Character] = Field(min_length=1)
    locations: list[Location] = Field(default_factory=list)
    scenes: list[Scene] = Field(min_length=1)

    @field_validator("characters")
    @classmethod
    def _unique_char_ids(cls, v: list[Character]) -> list[Character]:
        ids = [c.id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("人物 id 必须唯一")
        return v

    @field_validator("scenes")
    @classmethod
    def _unique_scene_ids(cls, v: list[Scene]) -> list[Scene]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("场景 id 必须唯一")
        return v


# =====================================================================
# 视频制作领域模型（v3.0 新增）
#
# 小说 → 剧本 → 分镜 → AI 短剧 全链路中的后半段数据结构。
# =====================================================================

# ---- 视频制作枚举 ----
ShotType = Literal[
    "extreme_wide", "wide", "medium", "close_up",
    "extreme_close_up", "over_shoulder", "pov",
]
CameraMoveType = Literal[
    "static", "pan", "tilt", "dolly", "tracking",
    "crane", "handheld", "zoom", "steady", "orbit",
]
CameraSpeed = Literal["slow", "medium", "fast"]
PacingType = Literal["slow", "medium", "fast", "variable"]
TransitionType = Literal["cut", "fade", "dissolve", "wipe", "none"]


class CameraMovement(BaseModel):
    """摄影机运动。

    只写 ``type``（推/摇/移）不足以让视频模型拍出想要的运动——它不知道镜头
    从哪儿出发、到哪儿停下。``path`` / ``height`` 把「摄影师规划好的运动轨迹」
    写成文字：把摄影机设想成一台贴近人物飞行的虚拟无人机，写清起点到终点的
    相对距离、机位高度与运动方式（前进/后退/侧移/环绕/升降/偏航/俯仰与惯性）。
    """

    type: CameraMoveType = "static"
    direction: str | None = None  # left / right / up / down / in / out
    speed: CameraSpeed = "medium"
    path: str = ""    # 运镜轨迹：起点 → 终点，含与主体的距离/角度变化与运动规律
    height: str = ""  # 机位高度：如「与人物胸口齐平，随后降到贴地」


class Shot(BaseModel):
    """镜头：场景拆解后的最小视频生成单元。

    每个镜头对应一段 5-10 秒的 AI 生成视频。
    ``id`` 格式为 ``shot_sceneXXX_NNN``。

    一致性锚定：文生视频每个镜头是独立采样，仅靠 prompt 无法跨镜锁住
    场景与人物。通过以下字段把「共享参考素材 / 首尾帧接力」显式建模：
      - reference_images/videos：贯穿全片的共享环境参考图、上一镜成片参考；
      - first/last_frame_image：首尾帧控制（镜头 N 尾帧 → 镜头 N+1 首帧）。
    """

    id: str = Field(pattern=r"^shot_[a-z0-9_]+_\d{3,}$")
    scene_id: str = Field(pattern=r"^scene_[0-9]{3,}$")
    order: int = Field(ge=0)
    shot_type: ShotType = "medium"
    camera: CameraMovement = Field(default_factory=CameraMovement)
    subject: str = ""
    action: str = ""
    dialogue_ref: str | None = None  # 关联 beat_xxx
    # 台词在镜头内第几秒开口（相对镜头起点，None=沿用配音层默认的「镜头起始后就开口」）。
    # 没有它，配音只能把所有台词排在镜头开头——画面在摸钱、声音已经在说下一句，
    # 观众对不上「这句是谁在什么时候说的」。
    dialogue_at: float | None = Field(default=None, ge=0.0, le=30.0)
    duration_sec: float = Field(default=5.0, ge=1.0, le=30.0)
    lighting: str | None = None
    mood: str | None = None
    # ---- 画面调度的文字证据：视频模型看不见导演的脑内画面，只能靠这些字段 ----
    spatial: str = ""            # 空间关系：人物编号、人物间相对距离、离镜头/地面多高，及其在运镜中的变化
    background_action: str = ""  # 背景人物各自独立、不同步的生活化行为（允许少数人静止）
    cut_reason: str = ""               # 为什么必须在这里切镜（写不出理由就该合并）
    # ---- 戏剧结构（先戏剧，后语法）----
    function: str = ""           # 三工作规则：emotion(改变情绪) / action(推进动作) / pressure(施加压力)，三件不做一件的镜头是壁纸
    final_frame: str = ""        # 最终画面：结束那一帧的可见状态——视频模型把它当情绪目的地
    # ---- 一致性计划（导演输出的符号化接力方案，运行时再解析为下面的 URL）----
    reference_group: str = ""          # 共享环境参考图分组键：同组镜头共用一张环境参考图
    chain_from: int | None = None      # 首帧接力：本镜首帧取自该 order 镜头的尾帧；None=序列起点/硬切
    # ---- 镜头组（一次生成 = 一段戏；2026-11-12 PLAN-shot-group S1）----
    # 同 group_id 的相邻镜头会被 DP 拼成一条 `[Shot N] At 00:XX cuts to…` 组提示词，
    # 一次提交生成。"" = 单镜自成一组（旧行为，逐字节兼容：旧版本数据没有这两个
    # 字段，pydantic 默认空值即单镜组）。组内切镜是同一次采样，不漂移——
    # 正反打/POV/过肩由此合法（厂商官方原生能力，见 D-46）。
    group_id: str = ""                 # 组键（导演给，如 grp_scene_001_003）；""=单镜组
    group_index: int = Field(default=0, ge=0, le=15)  # 组内机位序号（0 起，合法组≤4，宽松收）
    group_prompt: str | None = None    # 组提示词（仅组头镜头持有）：提交生成时用它，
                                       # 单镜 video_prompt 仍逐镜保留供审阅/审计/单镜重出片
    # ---- 一致性锚定（多模态输入，对接 VideoJobParams）----
    reference_images: list[str] = Field(default_factory=list)  # 共享环境/人物参考图 URL
    reference_videos: list[str] = Field(default_factory=list)  # 参考上一镜成片 URL，锁风格/运镜/光影
    reference_asset_ids: list[str] = Field(default_factory=list)  # 用户从参考素材里挑的参考视频 id
    first_frame_image: str | None = None  # 首帧图（接上一镜尾帧）
    last_frame_image: str | None = None   # 尾帧图（供下一镜接力）
    # ---- 视频生成状态 ----
    video_prompt: str | None = None
    prompt_audit: list[str] = Field(default_factory=list)  # 确定性提示词审计提醒（人工审阅时逐镜可见）
    video_url: str | None = None
    video_job_id: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> object:
        s = str(v).strip() if v is not None else ""
        if s.startswith("shot_"):
            return s
        return f"shot_{_slug_token(s) or hashlib.md5(str(v).encode()).hexdigest()[:8]}"

    @field_validator("scene_id", mode="before")
    @classmethod
    def _coerce_scene_id(cls, v: object) -> object:
        return normalize_id(v, "scene", fallback=v)

    @field_validator("dialogue_ref", mode="before")
    @classmethod
    def _coerce_dialogue_ref(cls, v: object) -> object:
        if v is None or str(v).strip() == "":
            return None
        return normalize_id(v, "beat", fallback=v)


class StyleGuide(BaseModel):
    """视觉风格指南：由美术指导 Agent 生成。

    ``reference_images`` 是图像级视觉锚的注册表：{人物名/地点名: 定妆图 URL}。
    由参考资产流水线（app/media/refs.py）生成并质检后回填，供镜头作为 reference_images 使用。
    """

    color_palette: list[str] = Field(default_factory=list)
    lighting_style: str = ""
    camera_style: str = ""
    # 画风（写实/二次元/三维/水墨）：决定定妆图 prompt 与视频提示词的画风声明。
    # 与 adaptation_type 正交——短剧可以是实拍，也可以做成漫剧。
    visual_style: VisualStyle = "realistic"
    visual_references: list[str] = Field(default_factory=list)
    character_appearances: dict[str, str] = Field(default_factory=dict)
    environment_descriptions: dict[str, str] = Field(default_factory=dict)
    reference_images: dict[str, str] = Field(default_factory=dict)

    @field_validator("visual_style", mode="before")
    @classmethod
    def _coerce_visual_style(cls, v: object) -> object:
        return normalize_visual_style(v)


class SceneAnchors(BaseModel):
    """五锚点（One-anchor 原则）：一场戏只承诺五个锚点，不多不少。

    锚点没命名的场景会「好看但空」——比堆形容词强得多的是这五个具体承诺，
    它们能分镜、能写 prompt、能剪成有分量的东西。
    """

    emotion: str = ""      # 一个主要情绪
    motif: str = ""        # 一个视觉母题：重复出现的感知钩子，贯穿本场多个镜头
    object: str = ""       # 一个锚定物体
    break_point: str = ""  # 一次破裂：击碎主角立场的那个细节
    final_image: str = ""  # 一个最终画面：观众带走的最后一帧


class SceneBreakdown(BaseModel):
    """场景拆解：由导演 Agent 生成，将一个 Scene 分解为多个 Shot。"""

    scene_id: str = Field(pattern=r"^scene_[0-9]{3,}$")
    director_notes: str = ""
    visual_approach: str = ""
    pacing: PacingType = "medium"
    key_moments: list[str] = Field(default_factory=list)
    anchors: SceneAnchors = Field(default_factory=SceneAnchors)
    shots: list[Shot] = Field(default_factory=list)

    @field_validator("scene_id", mode="before")
    @classmethod
    def _coerce_scene_id(cls, v: object) -> object:
        return normalize_id(v, "scene", fallback=v)


class VideoVersion(BaseModel):
    """视频版本快照：记录某次完整的分镜/镜头状态。"""

    id: str = Field(pattern=r"^vver_[a-z0-9_]+$")
    project_id: str
    parent_version_id: str | None = None
    source_type: str = "agent"  # agent / manual / generation
    label: str | None = None
    notes: str | None = None
    milestone: str | None = None  # draft / candidate / final
    shots: list[Shot] = Field(default_factory=list)
    style_guide: StyleGuide = Field(default_factory=StyleGuide)

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: object) -> object:
        s = str(v).strip() if v is not None else ""
        if s.startswith("vver_"):
            return s
        return f"vver_{_slug_token(s) or hashlib.md5(str(v).encode()).hexdigest()[:8]}"
