# =====================================================================
# story_plan.py —— 故事规划：理解卡 + 分集提案 + 按集扩写
#
# 解决什么（2026-11-20 对标即梦短剧工作室实操记录，D-71）：
# 我们的改编链路拿到原文直接开拆场景，集数被「时长预算」隐性决定——
# 短篇只够一集，装不下就删事件（《替身v2》复盘的病根之一：闸门删 16 镜、
# 核心台词整句消失）。即梦的路线是反的：先读原文产出「理解卡」
# （内核/人物/冲突/关键事件），再提案「做几集、每集讲什么、结尾钩子」，
# **用户确认后**才按集扩写——扩写是加料，事件是底线，一个不许删。
#
# 三件事：
#   plan_story()          理解 + 提案（一次结构化 LLM 调用），落库
#                         projects.story_plan_json，status=proposed
#   generate_from_plan()  按已确认的提案逐集扩写：每集只拍分给它的
#                         事件 + 前情提要，复用 generation 的两阶段
#
# 对话工具层把这套暴露成「先理解、再问、后写」的循环：Agent 提案、
# 用户确认（confirm 走对话，不做服务端硬闸门——生剧本不花钱，
# 花钱的批准闸门 D-43 在出片那一步，口径不变）。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# 单集目标时长（秒）：短剧一集 55–95 秒（即梦实操口径，也是我们的
# 内容比例预算「每场保底 24 秒」折算 2–4 场/集的区间）。
EPISODE_TARGET_SEC = 75.0
EPISODE_MIN_SEC = 55.0
EPISODE_MAX_SEC = 95.0


class PlanCharacter(BaseModel):
    """理解卡里的人物条目（bio 照抄原文设定，不编）。"""

    name: str = ""
    role: str = "supporting"      # protagonist / antagonist / supporting
    bio: str = ""


class StoryEvent(BaseModel):
    """原文的一个关键事件——扩写的底线单位，一个不许丢。"""

    id: str = ""                  # ev_001（代码兜底补号）
    text: str = ""                # 事件本身（含原文关键台词/道具，一句可拍）
    episode: int = 1              # 提案分配到第几集


class EpisodeArc(BaseModel):
    """一集的提案：讲什么、钩子落在哪。"""

    index: int = 1
    title: str = ""
    hook: str = ""                # 本集结尾的悬念/反转（短剧留人的关键）
    target_sec: float = EPISODE_TARGET_SEC
    scenes_hint: int = 4          # 扩写成几场（提案值，导演按内容定）


class StoryUnderstanding(BaseModel):
    logline: str = ""
    themes: list[str] = Field(default_factory=list)
    characters: list[PlanCharacter] = Field(default_factory=list)
    core_conflict: str = ""
    events: list[StoryEvent] = Field(default_factory=list)


class StoryPlan(BaseModel):
    """理解卡 + 分集提案。status=proposed 等用户拍板，confirmed 才进扩写。"""

    understanding: StoryUnderstanding = Field(default_factory=StoryUnderstanding)
    episodes: list[EpisodeArc] = Field(default_factory=list)
    rationale: str = ""
    status: str = "proposed"

    def normalized(self) -> "StoryPlan":
        """确定性纠偏（模型提案的脏数据在这一步归位，不打回重烧）：
        集号连续化、事件 episode 越界并入末集、事件 id 补号去重。"""
        if not self.episodes:
            self.episodes = [EpisodeArc(index=1, title="第 1 集")]
        self.episodes.sort(key=lambda e: e.index)
        for i, ep in enumerate(self.episodes, 1):
            ep.index = i
        n = len(self.episodes)
        seen: set[str] = set()
        for i, ev in enumerate(self.understanding.events, 1):
            if not ev.id or ev.id in seen:
                ev.id = f"ev_{i:03d}"
            seen.add(ev.id)
            if not 1 <= ev.episode <= n:
                ev.episode = n
        if self.status not in ("proposed", "confirmed"):
            self.status = "proposed"
        return self

    def events_of(self, episode_index: int) -> list[StoryEvent]:
        return [e for e in self.understanding.events if e.episode == episode_index]

    def to_card(self) -> str:
        """人话理解卡（对话里给用户的提案原文，即梦同款版式）。"""
        u = self.understanding
        lines = [
            f"故事内核：{u.logline}",
            f"核心冲突：{u.core_conflict}",
        ]
        if u.characters:
            lines.append("人物：" + "；".join(
                f"{c.name}（{c.bio[:30]}）" if c.bio else c.name
                for c in u.characters[:8]))
        lines.append(f"关键事件：共 {len(u.events)} 件（扩写底线，一个不删）")
        lines.append("")
        lines.append(f"分集提案：共 {len(self.episodes)} 集")
        for ep in self.episodes:
            ev_n = len(self.events_of(ep.index))
            lines.append(
                f"  第 {ep.index} 集《{ep.title}》：{ev_n} 件事件，"
                f"约 {ep.target_sec:.0f} 秒，钩子：{ep.hook or '（未给）'}")
        if self.rationale:
            lines.append(f"\n为什么这么分：{self.rationale}")
        return "\n".join(lines)


PLAN_PROMPT = (
    "你是短剧编剧兼策划。先「读懂」原文，再「提案」怎么分集——这一步不写剧本。\n"
    "\n"
    "【理解卡】\n"
    "- logline：一句话故事；\n"
    "- themes：主题词（2-4 个）；\n"
    "- core_conflict：核心冲突一句话；\n"
    "- characters：原文每个人物 {name, role, bio}，bio 照抄原文设定"
    "（年龄/身份/性格/关系），原文没给的留空，**不要编**；\n"
    "- events：**把原文的全部关键事件按原文顺序列全**，每条一句话、可直接拍："
    "台词事件写成「谁对谁说了什么（原话大意）」，道具/事故/发现单独成条。"
    "这份清单是扩写的底线——之后一集都不会删掉任何一条。\n"
    "\n"
    "【分集提案】\n"
    "- 每集 55–95 秒；集数 = 事件拍完需要的总时长 ÷ 约 75 秒，向上取整，至少 1 集；"
    "一部 7-10 场的短片通常 2–4 集；\n"
    "- 每个 event 标 episode（分给第几集）：同一地点/连续时间/同一冲突单元的事件"
    "进同一集，集与集的切换落在冲突转折处；\n"
    "- 每集 {index, title, hook, target_sec, scenes_hint}：title 短而有钩子感，"
    "hook 写清本集结尾留什么悬念，scenes_hint 是扩写成几场（事件薄的集要加戏："
    "反应、铺垫、日常细节、伏笔——**加料，不是删事件**）。\n"
    "\n"
    "【扩写原则（提案必须体现）】\n"
    "- 薄原文做厚靠加戏，不靠砍事件；\n"
    "- 钩子落在反转或新信息上，第一集开场 3 秒内要有事发生；\n"
    "- rationale 用人话解释为什么分这么多集。\n"
)


def plan_story(
    llm: Any,
    *,
    title: str,
    raw_text: str,
    episodes_hint: int | None = None,
    notes: str = "",
) -> StoryPlan | None:
    """理解 + 分集提案（一次结构化调用）。模型不可用/失败返回 None。

    episodes_hint：用户已拍板集数时带上（提案直接按这个集数分），
    notes：用户的额外要求（如「结尾留白」「第 2 集压缩」）。
    """
    if not getattr(llm, "available", False):
        return None
    from ..llm import LLM  # noqa: F401  (类型提示用，运行时不依赖)

    user = f"标题：{title}\n\n原文：\n{raw_text[:12000]}"
    if episodes_hint:
        user += f"\n\n用户已指定：做 {episodes_hint} 集。按这个集数分配事件。"
    if notes:
        user += f"\n\n用户要求：{notes}"
    try:
        resp = llm.structured(StoryPlan).invoke([
            {"role": "system", "content": PLAN_PROMPT},
            {"role": "user", "content": user},
        ])
        plan = resp if isinstance(resp, StoryPlan) else StoryPlan.model_validate(resp)
        if not plan.understanding.events:
            log.warning("故事提案没有事件清单，视为失败")
            return None
        return plan.normalized()
    except Exception as e:  # noqa: BLE001
        log.warning("故事提案失败：%s", str(e)[:160])
        return None


def episode_brief(plan: StoryPlan, ep: EpisodeArc) -> str:
    """一集的扩写任务书（拼进场景生成提示词的 scope_note）。"""
    events = plan.events_of(ep.index)
    lines = [
        f"【本集任务】第 {ep.index} 集《{ep.title}》，扩写成约 {ep.scenes_hint} 场，"
        f"合计约 {ep.target_sec:.0f} 秒。",
        "【事件底线——以下事件一个不许删、不许合并成流水账，每件都要有画面】",
    ]
    for ev in events:
        lines.append(f"  {ev.id}：{ev.text}")
    if ep.hook:
        lines.append(f"【结尾钩子】本集最后一个节拍要落在：{ep.hook}")
    lines.append(
        "【扩写规则】事件薄的部分加戏（反应/铺垫/伏笔/日常细节），加出来的戏"
        "必须服务本集冲突；禁止为了压时长删事件——装不下是提案的事，不是你的事。"
    )
    return "\n".join(lines)
