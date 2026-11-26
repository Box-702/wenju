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
import re
from typing import Any

from pydantic import BaseModel, Field

from ..video.rhythm import EPISODE_MAX_SEC, EPISODE_MIN_SEC, EPISODE_TARGET_SEC

log = logging.getLogger(__name__)

# 单集目标时长（秒）统一由 app/video/rhythm.py 提供（D-90）：
# 55–95 秒/集，默认 75。这里只 re-export，避免两套口径再次漂移。


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
    "\n"
    "只返回 JSON 对象（不要 Markdown 代码块）：\n"
    '{"understanding":{"logline":"","themes":[],"characters":[{"name":"","role":"","bio":""}],'
    '"core_conflict":"","events":[{"id":"ev_001","text":"","episode":1}]},'
    '"episodes":[{"index":1,"title":"","hook":"","target_sec":75,"scenes_hint":4}],'
    '"rationale":"","status":"proposed"}\n'
)


def plan_story(
    llm: Any,
    *,
    title: str,
    raw_text: str,
    episodes_hint: int | None = None,
    notes: str = "",
    dialogue_style: str = "",
) -> StoryPlan | None:
    """理解 + 分集提案（一次结构化调用）。模型不可用/失败返回 None。

    episodes_hint：用户已拍板集数时带上（提案直接按这个集数分），
    notes：用户的额外要求（如「结尾留白」「第 2 集压缩」），
    dialogue_style：用户已拍板的台词语体——提案里引用的关键台词也要按这个语体转述。
    """
    if not getattr(llm, "available", False):
        return None
    from ..llm import LLM  # noqa: F401  (类型提示用，运行时不依赖)

    user = f"标题：{title}\n\n原文：\n{raw_text[:12000]}"
    if episodes_hint:
        user += f"\n\n用户已指定：做 {episodes_hint} 集。按这个集数分配事件。"
    if notes:
        user += f"\n\n用户要求：{notes}"
    if dialogue_style:
        from .source_style import dialogue_style_note

        note = dialogue_style_note(dialogue_style)
        if note:
            user += f"\n\n{note}\n（提案 events 里引用的关键台词，也按这个语体转述。）"
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
        "【扩写规则——加戏边界】事件薄的部分可以加戏（反应/铺垫/伏笔），"
        "但**加出来的每一场都必须直接服务本集核心冲突或人物弧线**；"
        "纯日常过场、无冲突的寒暄和转场不许加。"
        "禁止为了压时长删事件——装不下是提案的事，不是你的事。"
    )
    return "\n".join(lines)


def _name_key(name: str) -> str:
    """人物跨集合并用的名字键：甲乙丙→一二三、去掉「众/们」等泛称前缀。

    逐集生成时 Bible 会把同一人物叫成「客一/客甲」「众门人/门人」；
    按原名精确匹配会拆成两个角色，下游多生定妆照。
    """
    s = str(name or "").strip()
    for a, b in (("甲", "一"), ("乙", "二"), ("丙", "三"), ("丁", "四")):
        s = s.replace(a, b)
    if s.startswith("众"):
        s = s[1:]
    return s.replace("们", "")


def _location_description_similarity(a: Any, b: Any) -> float:
    """两条地点描述的重合度（0–1），用于跨集合并同义地点。

    逐集生成会把同一座殿叫成「观宇」「劳山道观」，名字匹配认不出来；
    但描述都是「山顶…蒲团、案几、油灯、墙壁」，重合度高。2026-11-26
    实测：观宇 vs 劳山道观 0.414，其余地点对 ≤0.217——阈值取 0.35，
    既能并掉同义殿宇，又不会把「观中偏房」并进大殿。
    """
    import difflib

    def _norm(text: Any) -> str:
        return re.sub(r"[，。；、：:（）()\s]", "", str(text or ""))

    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


LOCATION_MERGE_SIMILARITY = 0.35


def _match_name_key(name: str, keys: Any) -> str | None:
    key = _name_key(name)
    if key in keys:
        return key
    # 「妻 / 王妻」「门人 / 众门人」这类包含关系；只允许长度差 ≤2，避免乱并。
    for existing in keys:
        if existing and key and abs(len(existing) - len(key)) <= 2 and (
            existing.endswith(key) or key.endswith(existing)
            or existing.startswith(key) or key.startswith(existing)
        ):
            return existing
    return None


def _strip_char_ids(text: str) -> str:
    """去掉动作里漏出的人物 id（模型偶尔写「王生（char_001）推门」）。"""
    cleaned = re.sub(r"[（(]\s*char_[A-Za-z0-9_]+\s*[）)]", "", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _episode_recap(script: Any) -> str:
    """给下一集的前情提要：只带上一集真正发生的事，不重复原文。"""
    bits = [f"上一集：{script.logline}"]
    for sc in script.scenes:
        for b in sc.beats:
            text = (b.line or b.text or "").strip()
            if text:
                bits.append(text)
    return "\n".join(bits)[:900]


def _merge_episode_scripts(title: str, scripts: list[Any], plan: StoryPlan) -> Any:
    """把逐集剧本合并成一本：场号全局连续，人物/地点按名字去重，
    所有场头 episode 与 chapter_refs 指向各自集。"""
    from ..domain import DialogueLine

    base = scripts[0].model_copy(deep=True)
    chars_by_name: dict[str, Any] = {}
    locs_by_name: dict[str, Any] = {}
    merged_chars: list[Any] = []
    merged_locs: list[Any] = []
    scenes: list[Any] = []
    beat_seq = 0

    for script in scripts:
        cmap: dict[str, str] = {}
        lmap: dict[str, str] = {}
        for c in script.characters:
            key = _match_name_key(c.name, chars_by_name)
            if key is not None:
                cmap[c.id] = chars_by_name[key].id
                continue
            c2 = c.model_copy(deep=True)
            used = {x.id for x in merged_chars}
            if c2.id in used:
                c2.id = f"{c2.id}_{len(merged_chars) + 1}"
            merged_chars.append(c2)
            chars_by_name[_name_key(c2.name)] = c2
            cmap[c.id] = c2.id
        for loc in script.locations:
            if loc.name in locs_by_name:
                lmap[loc.id] = locs_by_name[loc.name].id
                continue
            # 同义地点按描述重合度归并（观宇 / 劳山道观）：不并的话 ep1 与
            # ep2 会给同一座殿出两套锚和两张图，跨集布景必漂（2026-11-26 复查）。
            same = next(
                (x for x in merged_locs
                 if _location_description_similarity(
                     getattr(loc, "description", ""), getattr(x, "description", "")
                 ) >= LOCATION_MERGE_SIMILARITY),
                None,
            )
            if same is not None:
                lmap[loc.id] = same.id
                continue
            l2 = loc.model_copy(deep=True)
            used = {x.id for x in merged_locs}
            if l2.id in used:
                l2.id = f"{l2.id}_{len(merged_locs) + 1}"
            merged_locs.append(l2)
            locs_by_name[l2.name] = l2
            lmap[loc.id] = l2.id

        # 场标题全局化：逐集生成都写「第 1 场」，合并后会出现 4 个「第 1 场」。
        # 只改通用场号，保留模型起的描述性标题。
        ep_scene_no: dict[int, int] = {}
        for sc in script.scenes:
            sc2 = sc.model_copy(deep=True)
            sc2.id = f"scene_{len(scenes) + 1:03d}"
            ep = int(getattr(sc2, "episode", 1) or 1)
            ep_scene_no[ep] = ep_scene_no.get(ep, 0) + 1
            if re.fullmatch(r"第\s*\d+\s*场", str(sc2.title or "").strip()):
                sc2.title = f"第{ep}集·第{ep_scene_no[ep]}场"
            sc2.action = [_strip_char_ids(x) for x in sc2.action]
            sc2.characters = [cmap.get(cid, cid) for cid in sc2.characters]
            sc2.location_id = lmap.get(sc2.location_id, sc2.location_id)
            # 同一场里逐字重复的台词只留第一句（审计会报「台词重复」）
            seen_lines: set[str] = set()
            kept_beats: list[Any] = []
            for b in sc2.beats:
                if b.type == "dialogue" and (b.line or "").strip():
                    key = re.sub(r"[\s，。！？!?…；;：:\"'「」『』“”]+", "", b.line)
                    if len(key) >= 6 and key in seen_lines:
                        continue
                    if len(key) >= 6:
                        seen_lines.add(key)
                kept_beats.append(b)
            sc2.beats = kept_beats
            for b in sc2.beats:
                # beat id 全局重排：逐集生成都从 beat_001 开始，合并后 158 个
                # beat 只有 13 个不重复 id（2026-11-26 复查）。导演一批拆 2 场时
                # 两场会出现同名 beat，dialogue_ref 指哪一场全靠猜。
                beat_seq += 1
                b.id = f"beat_{beat_seq:03d}"
                if b.speaker:
                    b.speaker = cmap.get(b.speaker, b.speaker)
                if b.type not in ("dialogue", "os") and b.text:
                    b.text = _strip_char_ids(b.text)
            sc2.dialogue = [
                DialogueLine(speaker=b.speaker, line=b.line)
                for b in sc2.beats
                if b.type == "dialogue" and b.speaker and b.line
            ]
            scenes.append(sc2)

    base.title = title
    base.scenes = scenes
    base.characters = merged_chars or base.characters
    base.locations = merged_locs or base.locations
    base.logline = plan.understanding.logline or base.logline
    # 集数与每集目标必须随剧本一起交给导演（D-90）。按集扩写的剧本没有
    # 段落级决策，但揣着提案的 4 集 × 75/90/90/60 秒——不折成 AdaptationPlan，
    # 导演只能按内容量估（《劳山道士》实测 421 秒/5 集），既超提案预算，
    # 又会在 assign_episodes 里按 95 秒/集再拆成 6–7 集（2026-11-26）。
    base.adaptation_plan = adaptation_plan_for(plan, base)
    return base


def adaptation_plan_for(plan: StoryPlan, script: Any) -> Any:
    """把已确认的分集提案折成导演预算（D-90 的 AdaptationPlan）。

    按集扩写不产段落决策，segments 留空；scene_targets 按「该场内容量」
    把本集 target_sec 分到各场（与闸门的分配口径一致）。集数取剧本里
    实际出现的集号，避免提案与实际场次对不上时预算按空集算。
    """
    from ..domain import AdaptationPlan
    from ..video.rhythm import EPISODE_TARGET_SEC, speech_need_seconds

    targets_by_ep = {
        int(ep.index): max(0.0, float(ep.target_sec or 0.0)) for ep in plan.episodes
    }
    scenes_by_ep: dict[int, list[Any]] = {}
    for sc in script.scenes:
        idx = int(getattr(sc, "episode", 1) or 1)
        scenes_by_ep.setdefault(idx, []).append(sc)

    def _scene_seconds(sc: Any) -> float:
        dialogue = "".join(
            (b.line or "") for b in sc.beats if getattr(b, "type", "") == "dialogue")
        speech = speech_need_seconds(dialogue) if dialogue.strip() else 0.0
        actions = sum(1 for b in sc.beats if getattr(b, "type", "") == "action")
        return speech + 2.5 * actions

    scene_targets: dict[str, float] = {}
    total_target = 0.0
    for idx in sorted(scenes_by_ep):
        scenes = scenes_by_ep[idx]
        target = targets_by_ep.get(idx) or EPISODE_TARGET_SEC
        total_target += target
        weights = {sc.id: max(0.1, _scene_seconds(sc)) for sc in scenes}
        weight_sum = sum(weights.values()) or 1.0
        for sid, weight in weights.items():
            scene_targets[sid] = target * weight / weight_sum

    return AdaptationPlan(
        segments=[],
        target_sec=total_target or EPISODE_TARGET_SEC,
        episode_count=max(1, len(scenes_by_ep)),
        hook=plan.episodes[0].hook if plan.episodes else "",
        rationale=plan.rationale,
        scene_targets=scene_targets,
    )


def ensure_plan_budget(project: Any, script: Any) -> bool:
    """给「按集扩写」的旧版本补导演预算，返回是否补过（就地改 script）。

    generate_from_plan 之前不写 AdaptationPlan，2026-11-26 之前落库的
    分集作品都没有；续跑/重新拆镜时从 projects.story_plan_json 重建，
    不要求重新生成剧本。
    """
    if getattr(script, "adaptation_plan", None) is not None:
        return False
    raw = getattr(project, "story_plan", None) or {}
    if not isinstance(raw, dict) or not raw.get("episodes"):
        return False
    try:
        plan = StoryPlan.model_validate(raw)
    except Exception:  # noqa: BLE001
        return False
    script.adaptation_plan = adaptation_plan_for(plan, script)
    return True


def generate_from_plan(
    llm: Any,
    settings: Any,
    *,
    title: str,
    raw_text: str,
    plan: StoryPlan,
    adaptation_type: str,
    language: str,
    prior_context: str = "",
    dialogue_style: str = "",
    on_progress: Any = None,
) -> tuple[Any, dict[str, Any]]:
    """按已确认提案逐集扩写，再合并成一本多集剧本。

    每集只拿到本集事件底线 + 上一集前情；场头 episode / chapter_refs 由代码
    钉死（不靠模型自觉），合并时场号全局连续、人物地点按名字去重。
    """
    if plan.status != "confirmed":
        raise ValueError("故事提案尚未确认，不能按集扩写")
    from .generation import generate_script

    scripts: list[Any] = []
    prev = prior_context
    for ep in plan.episodes:
        if callable(on_progress):
            on_progress(ep)
        brief = episode_brief(plan, ep)
        script, _art = generate_script(
            llm,
            settings,
            title=f"{title}（第 {ep.index} 集）",
            raw_text=raw_text,
            adaptation_type=adaptation_type,
            language=language,
            prior_context=prev,
            dialogue_style=dialogue_style,
            extra_scope=brief,
            # 逐集 Bible 只带本集出场人物——全人物表会让场景阶段把台词
            # 安到没出场的人物头上（2026-11-26 实测：妻在山上说门人台词）。
            bible_scope=(
                "【本集人物范围】人物表只列本集事件中实际出场的人物；"
                "其他集的人物不要写进人物表，也不要给没有出场的人物安排台词。\n"
                + brief
            ),
            skip_adaptation=True,
        )
        # 确定性收口：说话人必须在场。漏列的补进 characters，
        # 否则文本审计报「漏人物」、下游选角/人物锚会漏。
        for sc in script.scenes:
            speakers = {b.speaker for b in sc.beats if b.speaker}
            missing = sorted(speakers - set(sc.characters))
            if missing:
                sc.characters = sc.characters + missing
        for sc in script.scenes:
            sc.episode = ep.index
            sc.chapter_refs = [f"ep_{ep.index:03d}"]
        scripts.append(script)
        prev = _episode_recap(script)

    merged = _merge_episode_scripts(title, scripts, plan)
    artifacts = {
        "mode": "plan",
        "episodes": len(plan.episodes),
        "scenes": len(merged.scenes),
        "story_plan": {
            "episodes": len(plan.episodes),
            "events": len(plan.understanding.events),
        },
    }
    return merged, artifacts
