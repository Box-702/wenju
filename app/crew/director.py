# =====================================================================
# director.py —— 导演 Agent
#
# 职责：创意总控、场景拆解
# 输入：Script（完整剧本）
# 输出：list[SceneBreakdown]（每个场景的镜头拆解方案）
#
# 导演分析每场戏的戏剧目标、情绪曲线、节奏需求，
# 决定如何用镜头语言讲好这个故事。
# =====================================================================

from __future__ import annotations

import logging
import re
from typing import Any

from ..domain import (
    CameraMovement,
    SceneAnchors,
    SceneBreakdown,
    Script,
    Shot,
    assign_dialogue_ranges,
)
from ..llm import LLM
from ..skills import render_skill
from .base import CrewAgent, CrewTaskResult
from .dramaturgy import SHOT_FUNCTIONS, audit_breakdowns

log = logging.getLogger(__name__)

# 导演的系统提示词外置为技能文件：app/skills/director-breakdown/SKILL.md
# （改规则改 .md 即可，不必动代码；原因见 docs/DECISIONS.md D-12）
def director_system() -> str:
    """导演系统提示词（内容在 app/skills/director-breakdown/SKILL.md）。"""
    return render_skill("director-breakdown")


# 分镜连续性校验失败后的最大重做次数（有界，避免死循环）。
MAX_DESIGN_ITERATIONS = 3

# 每轮回喂给模型的批评条数上限。一次性丢十几条会让它顾此失彼。
MAX_CRITIQUE_SHOWN = 5

# 一次拆几个场景。超过这个数，模型输出的分镜 JSON 会超单次输出上限被截断
# （2026-11-01 实测：7 场整拆时截断在第一个场景中途；3 场也会截断，尤其
# 模型把 director_notes 写长的时候）。2 场是「调用次数」与「截断率」的平衡点。
MAX_SCENES_PER_BATCH = 2

# 台词朗读速度：中文约每秒 4 字（配音层 TTS 的常态语速）。
CHARS_PER_SEC = 4.0


def action_budget(duration_sec: float) -> int:
    """一个分镜能写几个「逗号级动作短语」。

    2026-11-20 起口径变了（对齐即梦分镜粒度）：分镜不再是「一整段戏」，
    而是**一个小事件**（一个主动作 + 两笔细节）。模型写得太长不再打回
    （打回 = 烧一轮导演调用重写），而是**由代码拆成相邻分镜**——
    见 ``split_action_chains``。保留这个函数是为了让校验器与技能文档
    继续有一把共同的尺子。

    旧口径是「时长 ÷ 3、最少 3 段」（分句计数），实测拦不住真正的过载：
    《替身v2》004_017 只有 3 个分句，却含 8 个动作（站起来/走到橱柜前/折锡纸/
    拉开柜门/塞进罐后/抽手/推回/关柜门），人物直接被挤出画外。
    """
    return MAX_PHRASES_PER_BEAT


def count_action_units(action: str) -> int:
    """数一个镜头里写了几段身体动作（按分号/换行切，过短的碎片不计）。"""
    parts = re.split(r"[；;\n]+", action or "")
    return len([p for p in parts if len(p.strip()) >= 4])


def count_action_phrases(action: str) -> int:
    """数一个镜头里写了几句「逗号级动作短语」——拆链与过载判定用同一把尺子。

    分句数（``count_action_units``）与真实动作数不是一回事：模型写一个动作
    习惯补两笔细节（「把碗搁在柜台上；下巴一抬；手指点过去」是同一个动作的
    三步），而《替身v2》004_017 只有 3 个分句却含 8 个动作（逗号级）。
    粒度以逗号级为准。
    """
    return len(_split_phrases(action))


# 位移级大动作词表（动作下限校验用，2026-11-12 v3「83 秒重复一个动作」复盘）：
# 全身或大臂的运动才算——「抿嘴/抬眼/指节发白」这类微动作不在表内。
_MOVEMENT_RE = re.compile(
    r"起身|站起|坐下|坐回|站直|跨|迈步|走|绕过|绕到|挪开|俯身|弯腰|直起身|"
    r"后退|退后|退到|上前|凑近|转身|转开|甩开|抡|挥|推开|拉开|掀开|掀|踢|"
    r"探身|顿|砸|拍|敲|举起|放下|端起|端|递|抓|扑|靠上|倚上|摔"
)


def _default_pacing(adaptation_type: str | None) -> str:
    """导演没给 pacing 时的兜底档位。短剧 = fast（短视频时代不让观众等）。"""
    from ..video.rhythm import default_pacing

    return default_pacing(adaptation_type)


def _budget_for(adaptation_type: str | None, scene_count: int = 0) -> float:
    """成片目标时长（秒）：内容比例预算。

    2026-11-20《替身v2》复盘改的口径：旧版固定 120 秒不看内容量，10 场 52 个
    节拍被闸门「删掉 16 个镜头」——被删的事件恰恰是最需要独立镜头的，一个镜
    塞两个事件就是漂移之源（人物挤出画外、动作不发生）。现在每场保底
    MIN_SCENE_BUDGET_SEC 秒（≈5 个分镜），预算 = max(类型预算, 场数 × 保底)；
    超出一集时长的部分由分集摊开（见 assign_episodes 的按时长拆集），
    不再由删镜摊开。即梦对同一篇原文的路线就是这样（5 集 × 60–95 秒）。
    """
    from ..video.rhythm import content_duration_budget
    return content_duration_budget(adaptation_type, scene_count)


# 确定性闸门用的单镜时长上限：短剧节奏基线（导演技能「不要写 8 秒以上」）。
# ⚠️ 这只是**节奏基线**，不是硬上限：带台词的镜头要念得完就允许更长
# （见 enforce_duration_budget 的封顶逻辑）。2026-11-19 实测：把 8 秒的
# 台词镜硬砍到 6 秒 → 25 字需要 6.5 秒 → 台词被片尾截断（用户看片：
# 「第一镜话说不完」）。
CAP_SHOT_SEC = 6.0

# 单镜时长硬上限：目标视频模型一次生成的可行区间上界（4–15 秒）。
# 台词再长也不能超过它——超了模型直接拒单。
MAX_GENERATABLE_SEC = 15.0

# 闸门删镜时，每场戏至少保留几个镜头。0 = 不保底（旧行为：整场无台词的戏
# 会被按「无台词优先删」连根拔掉——2026-11-19 实测《替身》：整场《偷偷的甜》
# 6 个镜全被删光，观众看到的是「β 尝到甜味 → 直接和妻子喝咖啡」，故事断裂）。
MIN_SHOTS_PER_SCENE = 2

# 静态机位占比上限：超过即「躺平」，与 DIR-11 的批评阈值同源。
MAX_STATIC_RATIO = 0.75


def dialogue_need_sec(lines: list[Any] | None) -> float:
    """一段台词的口播所需秒数（**含 1 秒表演余量**）。

    口径统一到 ``app.video.rhythm.speech_need_seconds``（导演校验、片长闸门、
    摄影指导、导入链四处共用一把尺子）。之前三处各算各的，结果是闸门那把
    尺子总是最松——它把镜头砍到念不完，没人再复检（2026-11-19 实测《替身》：
    25 字台词需要 7.2 秒，被砍到 6 秒，成片第一句就是半句话）。
    """
    from ..video.rhythm import speech_need_seconds

    text = "".join((getattr(l, "line", "") or "") for l in (lines or []))
    return speech_need_seconds(text)

# 兜底指派池：五种动机各配一型（用户口径 2026-11-14：多数镜头只需记录
# 「从哪拍谁」，正常切镜就够；运镜配给有动机的镜头，且相邻镜头不重型）。
_MOTION_ASSIGNMENTS = (
    ("dolly", "in", "向主体缓推，逼近内心的变化"),
    ("dolly", "out", "缓慢拉远，揭示主体所处的完整处境"),
    ("pan", "right", "轻摇跟随视线，把注意力递给画外"),
    ("tilt", "up", "由道具上摇至面孔，让物件先说话"),
    ("tracking", "left", "侧移跟随走位，保持人物在画面同一侧"),
)


def enforce_motion_ratio(
    breakdowns: list[SceneBreakdown],
) -> tuple[list[SceneBreakdown], list[str]]:
    """确定性运镜闸门 v2（2026-11-14 认知修正）：**静止是基底，运镜是配给**。

    v1 的问题（用户看片指出「运镜刻意且重复」）：只按占比补运动、只会交替
    推/拉——把「镜头不动」治成了「每镜都在推」，模板句逐字重演三次。
    视频模型没有「这台相机刚才已经推过了」的记忆，运镜词是套路召唤，
    写越密越复读。所以 v2：
      · 只给**有动机的镜**配运镜（function=emotion/pressure 或带台词转折）——
        答不出「什么变了」的镜保持 static，这是选择不是躺平；
      · 逐镜**换型**（推→拉→摇→摇上→移），且不与前一运动镜同型；
      · 动机镜不够补到阈值就**接受更高静态占比**——宁可静止，不做无动机运动。
    回喂批评管不住才走代码（D-50）；改的是 camera 字段，正文由 DP 兜底拼。
    """
    notes: list[str] = []
    all_shots = [s for bd in breakdowns for s in bd.shots]
    n_all = len(all_shots)
    if n_all < 6:
        return breakdowns, notes
    statics = [s for s in all_shots
               if (s.camera.type or "static") in ("static", "steady", "")]
    if len(statics) / n_all <= MAX_STATIC_RATIO:
        return breakdowns, notes
    need = len(statics) - int(n_all * MAX_STATIC_RATIO)

    def has_motive(s: Shot) -> bool:
        return (str(s.function or "") in ("emotion", "pressure")
                or bool((s.dialogue_ref or "").strip()))

    # 按 order 走一遍：动机静态镜才配，型不与相邻运动镜重复。
    ordered = sorted(all_shots, key=lambda s: s.order)
    pool_idx = 0
    prev_move_key: tuple[str, str] = ("", "")
    converted = 0
    for s in ordered:
        moving = (s.camera.type or "static") not in ("static", "steady", "")
        if moving:
            prev_move_key = (s.camera.type, (s.camera.direction or "").strip())
            continue
        if converted >= need or not has_motive(s):
            continue
        # 选型：跳过与前一运动镜同型的候选
        for _ in range(len(_MOTION_ASSIGNMENTS)):
            cand = _MOTION_ASSIGNMENTS[pool_idx % len(_MOTION_ASSIGNMENTS)]
            pool_idx += 1
            if (cand[0], cand[1]) != prev_move_key:
                break
        mtype, mdir, path = cand
        s.camera.type = mtype
        s.camera.direction = mdir
        s.camera.path = path
        s.camera.speed = "slow"
        prev_move_key = (mtype, mdir)
        converted += 1
    if converted:
        notes.append(
            f"闸门：固定机位占比 {len(statics)}/{n_all} 超标，"
            f"{converted} 个有动机镜头已配运镜（逐镜换型）"
        )
    elif len(statics) / n_all > MAX_STATIC_RATIO:
        notes.append(
            f"闸门：固定机位占比 {len(statics)}/{n_all} 超标，但无动机镜"
            "不补运动（静止是基底）——若观感仍像 PPT，是导演该重找转折点"
        )
    return breakdowns, notes


def dialogue_map_for(script: Any, breakdowns: list[SceneBreakdown]) -> dict[str, list]:
    """全局「镜头 → 它该念的台词」映射。

    口径与校验层完全一致（``assign_dialogue_ranges``）：闸门要用它判断
    「这一镜念得完吗」，校验层要用它判断「这一镜是不是念不完」——
    两处必须同一把尺子，否则永远是闸门赢。
    """
    scene_of = {sc.id: sc for sc in (script.scenes if script else [])}
    out: dict[str, list] = {}
    for bd in breakdowns:
        scene = scene_of.get(bd.scene_id)
        shots = sorted(bd.shots, key=lambda s: s.order)
        out.update(assign_dialogue_ranges(shots, scene.beats if scene else []))
    return out


def scene_content_weights(script: Any) -> dict[str, float]:
    """每场戏的「内容量」权重 = 台本里该场的节拍数。

    闸门按场分配预算时用它：内容多的场（对白多、动作多）该多给时长，
    内容少的场该少给。**按镜头数平摊会把叙事重心摊平**——那正是
    「故事不连贯」的另一种来源。
    """
    return {sc.id: float(len(sc.beats or [])) for sc in (script.scenes if script else [])}


def _stretch_dialogue_shots(shots: list[Shot], dlg: dict[str, list]) -> int:
    """把念不完台词的镜头抬到「念得完」（≤15 秒硬上限），返回改了几个。"""
    stretched = 0
    for s in shots:
        need = dialogue_need_sec(dlg.get(s.id))
        if need and s.duration_sec < need:
            target = min(need, MAX_GENERATABLE_SEC)
            if target > s.duration_sec:
                s.duration_sec = target
                stretched += 1
    return stretched


def enforce_duration_budget(
    breakdowns: list[SceneBreakdown], budget_sec: float,
    *,
    shots_dialogue: dict[str, list] | None = None,
    scene_weights: dict[str, float] | None = None,
) -> tuple[list[SceneBreakdown], list[str]]:
    """确定性片长闸门：导演回喂多轮仍超预算时，代码直接砍。

    为什么必须有这一道：预算在提示词里写了、校验里也报了，但那些都只是「回喂批评」——
    模型可以不理，迭代用完照样放行（2026-11-12 孔乙己三次实测：45 / 39 / 53 镜，
    全部超 120 秒预算一倍以上）。超预算不只是节奏问题：每一镜都是一次真金白银的
    视频生成，53 镜意味着近三倍的算力与账户余额。批评管不住的数，代码来管。

    砍法（先便宜后疼）：
      1. 单镜时长封顶 CAP_SHOT_SEC——长镜头先瘦身；
         **但台词镜让路**：台词念不完的镜，砍到「念得完」为止（台词时长优先于
         节奏基线——2026-11-19 实测：25 字台词需要 6.5 秒，被砍到 6 秒就截断了，
         用户看片第一句就是「第一镜话说不完」）；
      2. 还超就删镜头。**按场分配预算删**（场权重 = 台本里该场的节拍数），
         优先删「壁纸镜头」（function 不在三工作里），同条件删时长最长的；
         每场至少留 MIN_SHOTS_PER_SCENE 镜，**「无台词」不再是删除理由**——
         没有台词不等于不重要（2026-11-19 实测《替身》：整场《偷偷的甜》6 个
         无台词动作镜被连根拔掉，观众看到的是「尝到甜味 → 直接和妻子喝咖啡」，
         故事断裂）；
      3. 删完修复 chain_from 悬空引用：指向被删 order 的改指向最近的保留前镜，
         没有前镜可指就断开接力（None）。
    返回 (压缩后的分镜, 人话说明列表)。调用方负责把说明拼进 summary。
    """
    notes: list[str] = []
    all_shots = [s for bd in breakdowns for s in bd.shots]
    if not all_shots:
        return breakdowns, notes
    dlg = shots_dialogue or {}
    weights = scene_weights or {}

    total = sum(max(0.0, s.duration_sec) for s in all_shots)

    # 1) 时长封顶（台词镜让路：封到「念得完」为止）
    #    注意：**片长没超预算时这一步也要做**——总时长 120 秒不等于每句台词
    #    都念得完（2026-11-19 实测：6 秒的镜念 25 字，片长没超，台词被截断）。
    capped = 0
    kept_long = 0
    for s in all_shots:
        if s.duration_sec <= CAP_SHOT_SEC:
            continue
        need = dialogue_need_sec(dlg.get(s.id))
        floor = min(max(CAP_SHOT_SEC, need), MAX_GENERATABLE_SEC)
        if s.duration_sec > floor:
            s.duration_sec = floor
            capped += 1
        else:
            kept_long += 1
    total = sum(max(0.0, s.duration_sec) for s in all_shots)
    if capped:
        notes.append(f"闸门：{capped} 个超长镜头已封顶 {CAP_SHOT_SEC:.0f} 秒")
    if kept_long:
        notes.append(
            f"闸门：{kept_long} 个带台词的镜头保留超过 {CAP_SHOT_SEC:.0f} 秒"
            "（台词念得完优先于节奏基线）"
        )

    # 1b) 台词镜即使没超 6 秒，也可能压根念不完（模型给得就短）→ 抬到念得完。
    #     这一步在封顶之后做，因为封顶可能又把长台词镜压回去了。
    stretched = _stretch_dialogue_shots(all_shots, dlg)
    if stretched:
        notes.append(f"闸门：{stretched} 个台词镜的时长已抬到「念得完」的下限")

    total = sum(max(0.0, s.duration_sec) for s in all_shots)
    if total <= budget_sec:
        kept_all = list(all_shots)
        kept_orders_all = sorted({s.order for s in kept_all})
        kept_set_all = set(kept_orders_all)
        out_all: list[SceneBreakdown] = []
        for bd in breakdowns:
            bd.shots = sorted([s for s in bd.shots if s.order in kept_set_all],
                              key=lambda s: s.order)
            out_all.append(bd)
        return out_all, notes

    # 2) 删镜头直到进预算。**以生成单元为单位删**：同组机位是一段戏的多个视点，
    #    删半个组 = 毁整段戏（2026-11-12 PLAN-shot-group S1）。
    def unit_of(s: Shot) -> str:
        return s.group_id or f"__solo_{s.id}"

    def drop_priority(s: Shot) -> tuple:
        # 壁纸镜先删；同条件下时长长的先删（省得多）。**不再按「有无台词」排序**——
        # 无台词 ≠ 不重要（默片式动作戏全靠画面讲），按有无台词删会连根拔掉整场。
        wallpaper = 0 if s.function not in SHOT_FUNCTIONS else 1
        return (wallpaper, -s.duration_sec, s.order)

    def unit_priority(unit: list[Shot]) -> tuple:
        # 组内取最"可删"成员的分作为整组分
        return min(drop_priority(s) for s in unit)

    units: dict[str, list[Shot]] = {}
    for s in all_shots:
        units.setdefault(unit_of(s), []).append(s)
    kept_units: list[list[Shot]] = list(units.values())

    # 2a) 按场分配预算：场权重 = 台本里该场的节拍数（内容多的场多给时长）。
    #     没有权重就退回按镜头数平摊。
    scene_of_unit = {id(u): u[0].scene_id for u in kept_units}
    scene_ids = [bd.scene_id for bd in breakdowns]
    w = {sc: float(weights.get(sc, 0) or 0) for sc in scene_ids}
    if not any(v > 0 for v in w.values()):
        for u in kept_units:
            w[u[0].scene_id] = w.get(u[0].scene_id, 0) + 1
    w_total = sum(w.values()) or 1.0
    quota = {sc: budget_sec * w.get(sc, 0.0) / w_total for sc in scene_ids}

    def scene_duration(sc: str) -> float:
        return sum(s.duration_sec for u in kept_units for s in u if s.scene_id == sc)

    def scene_shot_count(sc: str) -> int:
        # ⚠️ 别写成 sum(len(u) ... for s in u if ...)——那会给组内每个成员
        # 都计一次整组长度（2 镜的组被算成 4），保底检查全部失真
        # （2026-11-19 实测：《囚禁》2 镜一场被算成 4，remaining=2 通过保底，
        # 一刀整场删空）。
        return sum(1 for u in kept_units for s in u if s.scene_id == sc)

    dropped: list[Shot] = []
    # 删除单位是「生成单元」（组）：一个组可能含 2–4 个机位，一刀下去
    # shot_count 直减 2+，**必须按「删完之后剩几镜」判断保底**，而不是按
    # 「现在有几镜」——2026-11-19 重拆实测：《囚禁》2 镜恰是一个组，
    # 「2 > 保底 2」为假本不该动，2c 的「> 1」却放行了一刀两镜，整场删空。
    def remaining_after(sc: str, victim: list[Shot]) -> int:
        return scene_shot_count(sc) - len(victim)

    # 2b) 逐场削到配额：先削「配额最紧的场」（相对超得最多的），每场保底留
    #     MIN_SHOTS_PER_SCENE 镜，绝不把整场戏删空。
    order_scenes = sorted(scene_ids, key=lambda sc: -(scene_duration(sc) - quota[sc]))
    for sc in order_scenes:
        while scene_duration(sc) > quota[sc]:
            cands = [u for u in kept_units if u[0].scene_id == sc]
            if not cands:
                break
            victim = min(cands, key=unit_priority)
            if remaining_after(sc, victim) < MIN_SHOTS_PER_SCENE:
                break  # 再删就跌破保底：这场到此为止
            kept_units.remove(victim)
            dropped.extend(victim)

    # 2c) 还超预算：这次允许跌破保底，但**每场至少留 1 镜**（整场删空 =
    #     故事断裂，2026-11-19 实测），并在 notes 里如实说明「保底也保不住」。
    over_again = sum(
        max(0.0, s.duration_sec) for u in kept_units for s in u) > budget_sec
    if over_again:
        for sc in order_scenes:
            while sum(max(0.0, s.duration_sec) for u in kept_units for s in u) > budget_sec:
                cands = [u for u in kept_units if u[0].scene_id == sc]
                if not cands:
                    break
                victim = min(cands, key=unit_priority)
                if remaining_after(sc, victim) < 1:
                    break
                kept_units.remove(victim)
                dropped.extend(victim)
    kept = [s for u in kept_units for s in u]

    if dropped:
        notes.append(
            f"闸门：仍超预算，删掉 {len(dropped)} 个镜头"
            f"（壁纸 {sum(1 for d in dropped if d.function not in SHOT_FUNCTIONS)} 个；"
            f"每场保底 {MIN_SHOTS_PER_SCENE} 镜，整场不删空），"
            f"压到 {sum(s.duration_sec for s in kept):.0f} 秒"
        )
    final_total = sum(s.duration_sec for s in kept)
    if final_total > budget_sec:
        notes.append(
            f"闸门：各场都到保底镜数仍超预算（{final_total:.0f} 秒 > {budget_sec:.0f} 秒）"
            "——宁可超时，也不删空叙事"
        )

    kept_orders = sorted({s.order for s in kept})
    kept_set = set(kept_orders)

    # 3) 重建各场的 shots 列表并修复悬空 chain_from
    kept_by_scene: dict[str, list[Shot]] = {}
    for s in kept:
        kept_by_scene.setdefault(s.scene_id, []).append(s)
    out: list[SceneBreakdown] = []
    repaired = 0
    for bd in breakdowns:
        shots = sorted(kept_by_scene.get(bd.scene_id, []), key=lambda s: s.order)
        for s in shots:
            if s.chain_from is not None and s.chain_from not in kept_set:
                earlier = [o for o in kept_orders if o < s.order]
                s.chain_from = earlier[-1] if earlier else None
                repaired += 1
        if not shots and bd.shots:
            continue  # 整场被删空的 breakdown 不再输出
        bd.shots = shots
        out.append(bd)
    if repaired:
        notes.append(f"闸门：修复 {repaired} 处悬空的接力引用")
    return out, notes


def _format_constraints(
    adaptation_type: str | None,
    visual_style: str | None,
    *,
    batch_scenes: int = 1,
    total_scenes: int = 1,
) -> list[str]:
    """把「类型节奏」与「画风」写成给导演的硬约束（拼在 user prompt 里）。

    为什么这两件事要进 prompt 而不是只留代码默认值：分镜时长是导演自己写的
    duration_sec，代码里的默认值只在它没写时才生效。不把节奏基线讲清楚，
    模型就会按「电影感」给 8–10 秒镜头——短剧观众早就划走了。
    """
    from ..pipeline.visual_styles import get_visual_style
    from ..video.rhythm import (
        MIN_SCENE_BUDGET_SEC,
        content_duration_budget,
        normalize_format,
        rhythm_table,
    )

    fmt = normalize_format(adaptation_type)
    table = rhythm_table(fmt)
    spec = get_visual_style(visual_style)
    budget = content_duration_budget(fmt, total_scenes)

    lines = [""]
    if fmt == "short_drama":
        # 分批拆解时，模型只看得到自己这一批的戏，所以预算按场数折算成
        # 「这一批有多少秒」。2026-11-20 起口径翻转：旧版是固定 120 秒摊到
        # 每场（10 场 = 每场 12 秒），逼模型「该删就删」——实测闸门删掉 16 个
        # 镜头、苏晴的核心台词整句消失，而被删的恰恰是最需要独立镜头的事件
        # （一个镜塞两个事件 = 人物挤出画外、动作不发生 = 漂移之源）。
        # 现在每场保底 MIN_SCENE_BUDGET_SEC 秒（≈5 个分镜，够拍完该场全部节拍），
        # 预算超一集时长的部分由分集摊开，不由删镜摊开。
        share = max(batch_scenes, 1) * MIN_SCENE_BUDGET_SEC
        lines += [
            "【类型约束】短剧：横屏 16:9（默认，工作台可切竖屏）、每集 1–2 分钟、前 3 秒必须抓住注意力。",
            f"【本批预算】这次要拆 {batch_scenes} 场，合计约 **{share:.0f} 秒、"
            f"{max(1, int(share / table['medium']))}–{max(1, int(share / table['fast']))} 个镜头**"
            f"（全片 {total_scenes} 场共约 {budget:.0f} 秒，超一集会自动分集）。"
            "这是**下限式预算**：把本场的节拍全部拍完，宁多勿删。",
            "【覆盖律（硬规则）】本场每个节拍都要有镜头承载：动作节拍给画面，"
            "对白节拍给说话人镜头。**禁止删事件**——删事件不是省时长，是把两个事件"
            "挤进一个镜头，视频模型做不完就会把人物挤出画外、让动作不发生。"
            "时长超了由分集解决，不由你解决。",
            f"【节奏基线】短剧单镜 {table['fast']:.0f}–{table['slow']:.0f} 秒：多数镜头写 "
            f"{table['fast']:.0f}–{table['medium']:.0f} 秒，只有情绪转折或重大信息才给 "
            f"{table['slow']:.0f} 秒。不要写 8 秒以上的镜头——那是电影节奏。",
            "【pacing 取值】没有明确理由一律写 fast；只有整场需要压抑或沉淀时才用 medium/slow。",
        ]
    else:
        lines.append(f"【节奏基线】单镜建议 {table['fast']:.0f}–{table['slow']:.0f} 秒。")

    lines.append(f"【画风】{spec['label']}：{spec['render_note']}。")
    if spec["voice_pool"] == "anime":
        lines.append(
            "【画风对镜头的影响】二维/动画画面在大幅运镜时结构容易崩：优先固定机位、小幅推近、"
            "角色局部动作（走位、转身、抬手）；少用环绕、快速摇移与大幅度跟拍。"
        )
    lines.append(f"【画面里不要出现】{spec['negative']}。")
    return lines


def _coerce_float(v: Any) -> float | None:
    """宽松解析模型给的秒数：非数字/负数一律当「没给」。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None

# 枚举容错：模型常给出非标准值（如 "push_in" / "close-up" / "OTS"）。
_SHOT_TYPES = {"extreme_wide", "wide", "medium", "close_up", "extreme_close_up", "over_shoulder", "pov"}
_CAMERA_TYPES = {"static", "pan", "tilt", "dolly", "tracking", "crane", "handheld", "zoom", "steady", "orbit"}
# 有实际位移/旋转的运镜（这些必须有 camera_path，否则模型只会瞎动）
_MOVING_CAMERA_TYPES = _CAMERA_TYPES - {"static"}
_CAMERA_SPEEDS = {"slow", "medium", "fast"}
_PACINGS = {"slow", "medium", "fast", "variable"}
_SHOT_ALIASES = {
    "wide_shot": "wide", "full": "wide", "full_shot": "wide", "wide_angle": "wide",
    "establishing": "wide", "establishing_shot": "wide", "long_shot": "wide",
    "medium_shot": "medium", "mid": "medium", "mid_shot": "medium",
    "closeup": "close_up", "close": "close_up", "reaction": "close_up", "insert": "close_up",
    "extreme_closeup": "extreme_close_up", "ecu": "extreme_close_up",
    "ots": "over_shoulder", "over_the_shoulder": "over_shoulder",
    "pov_shot": "pov", "first_person": "pov",
}
_CAMERA_ALIASES = {
    "push": "dolly", "push_in": "dolly", "pull": "dolly", "pull_out": "dolly",
    "dolly_in": "dolly", "dolly_out": "dolly", "track": "tracking", "follow": "tracking",
    "zoom_in": "zoom", "zoom_out": "zoom", "fixed": "static", "stable": "steady",
    "hand_held": "handheld", "handheld_camera": "handheld", "crane_up": "crane",
    "orbit_shot": "orbit", "arc": "orbit", "revolve": "orbit", "rotate_around": "orbit",
}


def _pick_enum(value: Any, allowed: set[str], aliases: dict[str, str], default: str) -> str:
    """把模型给的值规整到枚举内；不认识就用默认值兜底。"""
    v = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if v in allowed:
        return v
    return aliases.get(v, default)



def _critique_for(batch: list, critique: list[str]) -> list[str]:
    """挑出跟这批场景相关的批评条目。

    分批拆解时整片校验仍然做（跨场景的接力、重复构图要整体看），但回喂时
    只喂本批的——否则模型会去改自己这一批里根本没有的场景，越改越乱。
    """
    if not critique:
        return []
    ids = [sc.id for sc in batch]
    return [c for c in critique if any(i in c for i in ids)]


# 镜头组上限（一次生成 = 一个片段；MiniMax H3 单次生成 4-15s，见 D-46 / PLAN-shot-group）
# 2026-11-20：成员数 4 → 3（对齐即梦「一个片段 2–3 个分镜」：分镜多了，
# 单次生成的动作复杂度与时长都会失控；模型 2-3 个分镜一段时刚好）。
GROUP_MAX_SEC = 15.0
GROUP_MIN_SEC = 4.0
GROUP_MAX_MEMBERS = 3

# 硬切/转场断点：cut_reason 里出现这些词说明这是镜头语言的段落边界。
# cut_reason 描述的是「为什么切到本镜」——边界镜可以开新组（它之后的
# 同段镜头跟它是一段），但绝不并入前面那段。
_HARD_CUT_RE = re.compile(r"硬切|转场|闪回|插叙|hard\s*cut|flashback|match\s*cut", re.IGNORECASE)

# ---- 分镜粒度（2026-11-20 对齐即梦：一个小事件 = 一个分镜）----
#
# 分镜不再是「一整段戏」，而是**一个小事件**：一个主动作 + 两笔细节，
# 写成 2–3 个逗号级短语。相邻分镜成组打包成一次生成（= 片段）。
#
# 这条换掉的是旧口径「一镜演一整段戏、段数下限 3 段、上限 时长÷3」：
# 《替身v2》实测每镜平均 7.4 个逗号短语（≈8 个动作）挤在 4–6 秒里，
# 人物被挤出画外、动作根本没发生（004_017 只剩半张脸）。
MAX_PHRASES_PER_BEAT = 3
# 超过这个短语数就是「动作链过长」，由代码拆成相邻分镜（打回重写太贵）
MAX_PHRASES_BEFORE_SPLIT = 5
# 拆出来的每个分镜的时长地板（秒，2026-11-20 真实数据回测定标）：
# 4-5 秒镜切 3 段 = 1.3-1.67 秒分镜，既演不成一件事、合计又 <4 秒
# 凑不成一次生成（auto_group 拒组 → 碎成撞厂商下限的孤儿单镜）。
# 2 秒地板让 4 秒镜最多切 2 段（合计恰 4 秒能成组）；撑不起的短镜不拆。
MIN_BEAT_SEC = 2.0
# 台词落点：分镜的动作里出现这些词，说明这一段就是「开口说话」的那段
_SPEECH_PHRASE_RE = re.compile(
    r"开口|说话|低声|轻声|开口问|应声|回答|念|喊|叫|问道|答道|开口道|轻声说"
)


def _split_phrases(action: str) -> list[str]:
    """把一段动作描述拆成逗号级短语（保留原文措辞，只切分界）。"""
    parts = [p.strip() for p in re.split(r"[；;，,。\n]+", action or "") if p.strip()]
    return [p for p in parts if len(p) >= 3]


def _renumber_shots(breakdowns: list[SceneBreakdown], start_order: int) -> int:
    """拆分之后重排 order / id / chain_from，返回下一个可用 order。

    拆出来的兄弟分镜共用同一个旧 order（order 是 id 的一部分
    ``shot_{scene}_{order:03d}``），不重排会出现重复 id；chain_from 指着
    「更早的 order」，也要跟着重映射（指向被拆镜的第一段）。
    """
    flat = [s for bd in breakdowns for s in bd.shots]
    ordered = sorted(flat, key=lambda s: (s.order, s.beat_index))
    old_orders = [s.order for s in ordered]
    old_to_new: dict[int, int] = {}
    for s, old in zip(ordered, old_orders, strict=True):
        if old not in old_to_new:
            old_to_new[old] = start_order      # 该旧 order 的第一段承接旧号
        s.order = start_order
        s.id = f"shot_{s.scene_id or 'scene_000'}_{start_order:03d}"
        start_order += 1
    for s, _old in zip(ordered, old_orders, strict=True):
        if s.chain_from is not None and s.chain_from in old_to_new:
            s.chain_from = old_to_new[s.chain_from]
    return start_order


def split_action_chains(breakdowns: list[SceneBreakdown]) -> int:
    """把动作链过长的分镜拆成相邻分镜（一个分镜 = 一个小事件）。

    2026-11-20《替身v2》实测：模型给的一镜平均 7.4 个逗号短语（≈8 个动作），
    挤在 4–6 秒里——视频模型做不完，人物直接被挤出画外（004_017 只剩半张脸），
    动作也没发生。旧的解法是「打回让模型重写」，那要烧一整轮导演调用，
    而且模型往往照旧写长（教了不做）。

    现在改成**确定性拆分**：
      · 短语数 ≤ MAX_PHRASES_BEFORE_SPLIT 的不动（正常分镜）；
      · 超了的按短语数切成若干段，每段一个分镜（一个主动作 + 两笔细节）；
      · **总时长守恒**：拆出来的分镜时长之和 ≈ 原时长（出片单位是「片段」，
        片段总时长不变 → 片长预算与花费都不变）；
      · 台词落在「含开口/说话字样」的那一段；没有就落在最后一段（此时
        片段开头就是台词，不会被片尾截断）；
      · 运镜只留给第一段（整段动作的运镜轨迹不可复制到每一段），
        后续段切成静态机位（片段内切镜）。

    返回拆出来的分镜数。
    """
    split_total = 0
    for bd in breakdowns:
        new_shots: list[Shot] = []
        for s in bd.shots:
            phrases = _split_phrases(s.action)
            if len(phrases) <= MAX_PHRASES_BEFORE_SPLIT:
                new_shots.append(s)
                continue
            # 切成若干段：每段最多 MAX_PHRASES_PER_BEAT 个短语
            chunks = [
                phrases[i:i + MAX_PHRASES_PER_BEAT]
                for i in range(0, len(phrases), MAX_PHRASES_PER_BEAT)
            ]
            # 段数受时长约束（2026-11-20 真实数据回测抓到的核心 bug）：
            # 一个 4-5 秒的镜切 3 段 = 每段 1.3-1.67 秒，既演不成一件事，
            # 三段合计又 <4 秒凑不成一次生成（auto_group 拒绝成组 → 碎成
            # 单镜孤儿，撞厂商单次生成 4 秒下限）。所以每段至少 MIN_BEAT_SEC
            # 秒；装不下就**按允许的段数重新均分短语**（尾合并会造出
            # [3,5] 的不均衡段，回测抓到）。
            max_chunks = max(1, int(s.duration_sec // MIN_BEAT_SEC))
            if len(chunks) > max_chunks:
                per_p = -(-len(phrases) // max_chunks)      # ceil 均分
                chunks = [phrases[i:i + per_p] for i in range(0, len(phrases), per_p)]
            n = len(chunks)
            if n <= 1:
                # 时长撑不起拆分（太短的镜）：不拆，整镜保留（哪怕动作略挤），
                # 强拆只会造出演不成的碎片——原样交给导演/闸门处理更诚实。
                new_shots.append(s)
                continue
            # 时长守恒：精确均分，最后一段吃余数（片段合计 = 原时长，
            # 拆链兄弟同组 → 片段 ≥4 秒能成组，花费与片长都不变）。
            per = s.duration_sec / n
            tail_sec = s.duration_sec - per * (n - 1)
            # 台词落点：含「开口/说话」的那一段，否则最后一段
            dlg_idx = n - 1
            for i, ch in enumerate(chunks):
                if any(_SPEECH_PHRASE_RE.search(p) for p in ch):
                    dlg_idx = i
                    break
            for i, ch in enumerate(chunks):
                clone = s.model_copy(deep=True)
                clone.action = "，".join(ch)
                clone.duration_sec = round(tail_sec if i == n - 1 else per, 2)
                clone.beat_index = i
                clone.beat_total = n
                # 运镜只给第一段：整段动作的运镜轨迹不能复制到每一段
                if i > 0:
                    clone.camera = clone.camera.model_copy(
                        update={"type": "static", "path": ""}
                    )
                # 末帧只对最后一段有意义（动作链的结束态）
                clone.final_frame = s.final_frame if i == n - 1 else ""
                # 台词只落在一段上（domain 侧同 ref 只认第一个，这里显式化）
                clone.dialogue_ref = s.dialogue_ref if i == dlg_idx else None
                # 接力只给第一段（后续是同一次生成内的切镜，不是接力）
                clone.chain_from = s.chain_from if i == 0 else None
                # 切点理由：第一段沿用原由，后面的说明是同一动作链的下一步
                clone.cut_reason = (
                    s.cut_reason if i == 0
                    else "同一动作链的下一步：动作单元切换"
                )
                new_shots.append(clone)
            split_total += n - 1
        bd.shots = new_shots
    return split_total


def _normalize_groups(breakdowns: list[SceneBreakdown]) -> None:
    """归一化导演给的 group_id：非法的组**降级为单镜**（拆键），而不是打回。

    规则（就地修改）：
      · 组键按 **(scene_id, group_id)** 认——两个场景复用同名组键时绝不
        合并（组=一次生成，跨场合并等于把两段不相邻的戏塞进同一次调用）；
      · 同组镜头必须在全局 order 上连续——被隔开的拆成两组；
      · 组总时长必须落在 [GROUP_MIN_SEC, GROUP_MAX_SEC]——超了从尾部拆
        新组，不足最小体量整组拆回单镜（旧行为只在校验器里报告：模型
        拿不到批评就没人管，短组照样混出片）；
      · 组成员 ≤ GROUP_MAX_MEMBERS；
      · group_index 按组内顺序重排 0..n-1；
      · 单成员组清空 group_id（等价单镜，下游按单镜走）。
    校验器随后只查「组是否自洽」，不再因为分组问题回喂重拆——分组是
    锦上添花，不值得烧一整轮导演调用来修格式。"""
    flat: list[Shot] = sorted((s for bd in breakdowns for s in bd.shots),
                              key=lambda s: s.order)
    # 按 (场景, group_id) 分桶（保序）：组键只在场景内唯一
    buckets: dict[tuple[str, str], list[Shot]] = {}
    for s in flat:
        if s.group_id:
            buckets.setdefault((s.scene_id, s.group_id), []).append(s)

    for (_scene_id, gid), members in list(buckets.items()):
        if len(members) < 2:
            for s in members:
                s.group_id = ""
                s.group_index = 0
            continue
        # 连续性检查 + 时长/人数上限：切成若干合法子组
        parts: list[list[Shot]] = [[members[0]]]
        for prev, cur in zip(members, members[1:], strict=False):
            same_run = cur.order == prev.order + 1
            fits = (sum(x.duration_sec for x in parts[-1]) + cur.duration_sec <= GROUP_MAX_SEC
                    and len(parts[-1]) < GROUP_MAX_MEMBERS)
            if same_run and fits:
                parts[-1].append(cur)
            else:
                parts.append([cur])
        for pi, part in enumerate(parts):
            total = sum(x.duration_sec for x in part)
            if len(part) < 2 or total < GROUP_MIN_SEC:
                sub_gid = ""  # 单成员 / 达不到一次生成的最小体量 → 拆回单镜
            elif pi == 0:
                sub_gid = gid
            else:
                sub_gid = f"{gid}-{pi}"
            for si, s in enumerate(part):
                s.group_id = sub_gid
                s.group_index = si if sub_gid else 0
    # 单成员子组清键
    for s in flat:
        if s.group_id:
            n = sum(1 for x in flat if x.group_id == s.group_id)
            if n < 2:
                s.group_id = ""
                s.group_index = 0


def auto_group_scene(breakdowns: list[SceneBreakdown]) -> tuple[int, int]:
    """确定性成组兜底（2026-11-13 默认值翻转）：同一场内相邻的孤立单镜，
    只要塞得进一次生成的预算（[4,15]s、≤4 员），就自动打包成组。

    为什么不能只靠 SKILL 教：实测 deepseek 拿到「连续戏默认成组」的新规则
    后仍几乎不标 group（一场只标过一组）——和运镜、台词同一个病：教了不做。
    同场相邻镜头本来就是同一时空，成组只会更好不会更坏：组内锚只写一遍、
    不漂移、声音跨切镜；唯一代价是组太大时单次生成的动作复杂度，人数/秒数
    上限已经卡死。跨场绝不合并（地点/时间必然跳变）。

    语义边界（2026-11-14 收紧，防止「同场相邻」吞掉不该拼的）——
      · chain_from 显式接力：组内机位无法各自带接力锚，必须独立成单镜；
      · reference_group 变化：环境锚不同（硬切到新地点），不跨锚打包；
      · subject 变化：主体切换/交替对白各自成单镜（DP 折叠的人物锚按镜
        走，但自动成组保守起见不跨主体）；
      · cut_reason 是硬切/转场/闪回类断点：边界镜开新段，不并入前段。
    返回 (新成组的格数, 组数)。
    """
    grouped = 0
    n_groups = 0
    for bd in breakdowns:
        got, groups = _auto_group_one_scene(bd)
        grouped += got
        n_groups += groups
    return grouped, n_groups


def _auto_group_one_scene(bd: SceneBreakdown) -> tuple[int, int]:
    """单场内的自动成组（auto_group_scene 的逐场实现，见其函数头注释）。"""
    grouped = 0
    n_groups = 0
    shots = sorted(bd.shots, key=lambda s: s.order)
    run: list[Shot] = []

    def _flush() -> None:
        # 达不到一次生成的最小体量就不成组（保持单镜，不硬凑短组）
        nonlocal grouped, n_groups, run
        if len(run) >= 2 and sum(x.duration_sec for x in run) >= GROUP_MIN_SEC:
            gid = f"g_auto_{bd.scene_id}_{run[0].order:03d}"
            for si, x in enumerate(run):
                x.group_id = gid
                x.group_index = si
            grouped += len(run)
            n_groups += 1
        run = []

    for s in shots + [None]:  # 哨兵收尾
        if s is None:
            _flush()
            continue
        # 硬边界：已属模型给的组——本镜必须独立，不开新 run
        if s.group_id:
            _flush()
            continue
        # 拆链头开新段（2026-11-20 真实数据回测抓到）：一个原分镜拆出的
        # 兄弟必须同段——但拆链头**继承了原镜的 chain_from**（接力锚锚定的
        # 正是这段的开头），旧判定把它当「显式接力必须独立成镜」拦成单镜，
        # 兄弟段没了头就各自碎成 1.3 秒的孤儿片段。接力硬边界只适用于
        # 非拆链的独立镜。
        if s.beat_total > 1 and s.beat_index == 0:
            _flush()
            run = [s]
            continue
        # 硬边界：显式接力锚——本镜必须独立，不开新 run
        if s.chain_from is not None:
            _flush()
            continue
        # 软边界：不并入当前 run，但可以自己开新段（它之后的同段镜头跟它）
        soft_break = (
            bool(_HARD_CUT_RE.search(str(s.cut_reason or "")))
            or (run and s.reference_group != run[0].reference_group)
            or (run and str(s.subject or "").strip()
                != str(run[0].subject or "").strip())
        )
        if soft_break:
            _flush()
            run = [s]
            continue
        fits = (sum(x.duration_sec for x in run) + s.duration_sec <= GROUP_MAX_SEC
                and len(run) < GROUP_MAX_MEMBERS)
        if fits:
            run.append(s)
            continue
        _flush()
        run = [s]
    return grouped, n_groups



class DirectorAgent(CrewAgent):
    """导演 Agent：将剧本拆解为镜头方案。"""

    name = "director"
    label = "导演"
    emoji = "🎬"
    # 最近一次拆解的底层死因（网络/欠费等），随 summary 带出给 evals 分类用。
    _last_error: str | None = None

    def run(
        self,
        script: Script,
        *,
        scene_ids: list[str] | None = None,
        adaptation_type: str | None = None,
        visual_style: str | None = None,
        **kwargs: Any,
    ) -> CrewTaskResult:
        """执行场景拆解。

        Args:
            script: 完整剧本。
            scene_ids: 指定要拆解的场景 ID 列表；None = 全部。
            adaptation_type: 改编类型。短剧的默认 pacing 是 fast——短视频时代
                的注意力只给开头三秒，不能用电影的 medium 起步。
            visual_style: 画风（realistic/anime/3d/ink）。它不改节奏，只改镜头
                语法：二维画面的结构在大幅运镜时容易崩，提示词里要收着写。

        Returns:
            CrewTaskResult.data = list[SceneBreakdown]
        """
        if not self.llm.available:
            return self._fallback(script, scene_ids)

        # 筛选目标场景
        targets = script.scenes
        if scene_ids:
            targets = [sc for sc in script.scenes if sc.id in scene_ids]

        if not targets:
            return CrewTaskResult(success=False, summary="没有找到目标场景")

        default_pacing = _default_pacing(adaptation_type)

        # 分批拆解：一次输出整部戏的分镜 JSON 会超模型单次输出上限，被截断后
        # JSON 不闭合、解析必失败（2026-11-01 实测：7 场戏整拆时截断在 scene_001
        # 中途）。按场分批后每批只输出 2–3 场，长度可控；校验与回喂仍按全片做。
        batches = [targets[i:i + MAX_SCENES_PER_BATCH]
                   for i in range(0, len(targets), MAX_SCENES_PER_BATCH)]

        critique: list[str] = []
        last: list[SceneBreakdown] = []
        for _ in range(MAX_DESIGN_ITERATIONS):
            breakdowns: list[SceneBreakdown] = []
            order = 0
            for batch in batches:
                bds, order = self._design_batch(
                    script, batch, adaptation_type, visual_style,
                    default_pacing, order,
                    # 只把与本批相关的批评回喂：跨批的批评会让模型分心去改别的场。
                    critique=_critique_for(batch, critique),
                )
                if bds is None:
                    why = f"（{self._last_error}）" if self._last_error else ""
                    return CrewTaskResult(
                        success=False,
                        summary=f"导演工作失败：{'、'.join(sc.id for sc in batch)} 拆解失败{why}",
                        errors=critique,
                    )
                breakdowns.extend(bds)
            last = breakdowns
            critique = self._validate_continuity(
                breakdowns, script,
                budget_sec=_budget_for(adaptation_type, len(script.scenes)),
            )
            if not critique:
                # 校验通过（在 1.5x 上限内）也要过确定性闸门：
                # 闸门按「预算」而不是「上限」收——超预算的秒数就是超预算的钱。
                budget = _budget_for(adaptation_type, len(script.scenes))
                gated, notes = enforce_duration_budget(
                    breakdowns, budget,
                    shots_dialogue=dialogue_map_for(script, breakdowns),
                    scene_weights=scene_content_weights(script),
                )
                gated, motion_notes = enforce_motion_ratio(gated)
                notes.extend(motion_notes)
                summary = (
                    f"导演完成 {len(gated)} 个场景的镜头拆解，"
                    f"共 {sum(len(b.shots) for b in gated)} 个镜头"
                )
                if notes:
                    summary += "；" + "；".join(notes)
                assign_episodes(gated, script)
                return CrewTaskResult(data=gated, summary=summary)

        # 迭代用完仍不合格：批评可以不理，预算不能不理——钱是硬约束。
        budget = _budget_for(adaptation_type, len(script.scenes))
        gated, notes = enforce_duration_budget(
            last, budget,
            shots_dialogue=dialogue_map_for(script, last),
            scene_weights=scene_content_weights(script),
        )
        # 运镜是导演语言不是可选项：批评回喂不理的，代码兜底（5.44 同款纪律）。
        gated, motion_notes = enforce_motion_ratio(gated)
        notes.extend(motion_notes)
        total = sum(len(b.shots) for b in gated) if gated else 0
        summary = f"导演完成拆解（共 {total} 镜）"
        if notes:
            summary += "；" + "；".join(notes)
        if critique:
            summary += f"，但连续性校验仍待完善：{critique[0]}"
        assign_episodes(gated, script)
        return CrewTaskResult(data=gated, summary=summary, errors=critique)

    def _design_batch(
        self,
        script: Script,
        targets: list,
        adaptation_type: str | None,
        visual_style: str | None,
        default_pacing: str,
        start_order: int,
        *,
        critique: list[str] | None = None,
    ) -> tuple[list[SceneBreakdown] | None, int]:
        """拆一批场景，返回 (分镜列表, 下一个可用全局 order)。失败返回 (None, start_order)。"""
        user_prompt = self._build_prompt(script, targets, adaptation_type, visual_style)
        if critique:
            # 一次只回喂最要紧的几条：把十几条批评一起丢回去，模型会顾此失彼、
            # 越改越散（2026-10-31 实测：11 条全给时，问题从 2 个涨到 7 个）。
            shown = critique[:MAX_CRITIQUE_SHOWN]
            more = "" if len(critique) <= len(shown) else f"（另有 {len(critique) - len(shown)} 条次要问题，下一轮再说）"
            user_prompt += (
                f"\n\n上一版分镜的连续性校验发现 {len(critique)} 个问题{more}。"
                "先只改下面这几条，其余部分保持不动，改完重新输出完整 JSON：\n"
                + "\n".join(f"- {c}" for c in shown)
            )
        try:
            raw = self._invoke_json(director_system(), user_prompt)
            breakdowns = self._parse_breakdowns(
                raw, default_pacing=default_pacing, start_order=start_order,
            )
        except Exception as e:  # noqa: BLE001
            # 死因必须带出去（2026-11-13 eval 误判复盘）：402 欠费被吞成
            # 「拆解失败」后，evals 把基础设施故障判成规则稳定缺陷——
            # 排查方向整个被带偏。上层 summary 拼这条原文，_classify_block
            # 的 marker（402/insufficient balance）才够得着。
            log.warning("导演拆解失败（%s）：%s", "、".join(sc.id for sc in targets), e)
            self._last_error = f"{type(e).__name__}: {e}"[:200]
            return None, start_order

        # 截断救援后可能少几个场景：把没拆出来的单独补拆（一次一场，输出够短），
        # 别让「差一场」把整批判成失败。0 镜的场景同样算没拆出来。
        got = {b.scene_id for b in breakdowns if b.shots}
        missing = [sc for sc in targets if sc.id not in got]
        if missing and len(targets) > 1:
            for sc in missing:
                more, start_order = self._design_batch(
                    script, [sc], adaptation_type, visual_style, default_pacing, start_order,
                )
                if more:
                    breakdowns.extend(more)
                else:
                    log.warning("场景 %s 单独补拆仍失败", sc.id)
                    return None, start_order

        return breakdowns, start_order + sum(len(b.shots) for b in breakdowns)

    def _validate_continuity(
        self, breakdowns: list[SceneBreakdown], script: Script | None = None,
        *, budget_sec: float | None = None,
    ) -> list[str]:
        """确定性校验分镜（不靠固定镜头数，只查结构自洽、生成器可行性与戏剧结构）。

        检查项：
          1. 同场景多镜必须指定 reference_group，才能共享环境参考图；
          2. chain_from 只能接力到更早的镜头（不悬空、不自指、不向后指）；
          3. 单镜时长必须落在目标模型单次生成的可行区间（默认 4–15 秒）；
          4. 非首镜必须填写 cut_reason（写不出切镜理由就该合并）；
          5. 相邻镜头不应是同一个人的重复面部特写（AI 视频里最伤的重复切镜）；
          6. 运动镜头必须写 camera_path——写不出轨迹，模型就会自己乱动；
          6b.动作密度：一镜塞不下超过「时长 ÷ 3（最少 3 段）」个身体动作（action_budget 实测算法）；
          6c.一镜一说话人：同镜挤进多个对白角色，观众分不清是谁在说；
          6d.对白要有归处：每段对话都必须被某个镜头用 dialogue_ref 认领；
          7-10. 戏剧结构审计（先戏剧，后语法）：三工作 function、最终画面 final_frame、
             细节律三细节、场景五锚点——由 dramaturgy.audit_breakdowns 提供。
        """
        issues: list[str] = []
        all_orders = {s.order for bd in breakdowns for s in bd.shots}
        scene_of = {sc.id: sc for sc in (script.scenes if script else [])}
        char_of = {c.id: c.name for c in (script.characters if script else [])}

        # 0) 片长预算（2026-11-20 起是内容比例预算：max(类型预算, 场数×24s)）。
        #    没有这一条时，导演会按剧情一路拆下去——《孔乙己》七场戏拆出 60 镜
        #    295 秒，是短剧的三倍，生成成本与观众耐心都不允许。
        #    批评口径同步翻转：超预算不再要求「删事件」（那是漂移之源），
        #    而是「把挤在一起的动作拆开写短、把重复机位合并」——事件本身保留。
        if budget_sec:
            total = sum(max(0.0, s.duration_sec) for bd in breakdowns for s in bd.shots)
            limit = budget_sec * 1.5
            if total > limit:
                n = sum(len(bd.shots) for bd in breakdowns)
                issues.append(
                    f"整片总时长 {total:.0f} 秒、共 {n} 个镜头，超出预算（目标 {budget_sec:.0f} 秒，"
                    f"上限 {limit:.0f} 秒）。**不要删事件**：把时长压回节奏基线"
                    "（多数分镜 3–5 秒，别一律写 6 秒以上）、合并同一事件的重复机位；"
                    "每个节拍仍要有镜头承载。"
                )

        # 0b) 镜头语言多样性：模型会躺在「特写+固定机位」的安全区里——
        #     2026-11-12 孔乙己 v2 实测：25 镜里 11 个 close_up、0 个运动镜头
        #     （25/25 static），用户观感「全部正脸怼脸拍、运镜怪」。
        #     景别与运镜是导演的语言，不是防错成本——这里用确定性阈值逼一把。
        all_shots = [s for bd in breakdowns for s in bd.shots]
        n_all = len(all_shots)
        if n_all >= 6:
            n_close = sum(1 for s in all_shots
                          if s.shot_type in ("close_up", "extreme_close_up"))
            n_static = sum(1 for s in all_shots
                           if (s.camera.type or "static") in ("static", "steady", ""))
            n_wide = sum(1 for s in all_shots
                         if s.shot_type in ("extreme_wide", "wide"))
            if n_close / n_all > 0.4:
                issues.append(
                    f"特写占比 {n_close}/{n_all}（>40%）：面部特写是 AI 畸变最高发景别，"
                    "而且全片怼脸会看疲劳。把没有情绪转折的对话特写改成 medium 或 "
                    "over_shoulder（过肩镜头天然带空间关系），"
                    "close_up/extreme_close_up 只留给情绪顶点与关键道具。"
                )
            if n_static / n_all > 0.75:
                issues.append(
                    f"固定机位占比 {n_static}/{n_all}（>75%）：镜头不动=摄影机不存在。"
                    "逐镜重想一遍：这一镜在讲什么？逼近内心用缓推、揭示处境用拉远、"
                    "交代空间用平移/轻摇——对峙与凝固可以留静态，但全片静态不是选择是躺平。"
                    "改的每镜写 camera_path 起止，一镜只一种运镜。"
                )
            if n_wide == 0:
                issues.append(
                    "全片没有一个 wide/extreme_wide：场景空间从未交代，观众不知道人在哪。"
                    "每场的第一镜优先用 wide 或 medium 建立空间，再进近景。"
                )
        # 0c) 镜头组自洽（normalize 已保证，这里是第二只眼：防外部数据绕过归一化）
        # 组键按 (scene_id, group_id) 认，与归一化/生成单元折叠同口径。
        by_gid: dict[tuple[str, str], list[Shot]] = {}
        for s in all_shots:
            if s.group_id:
                by_gid.setdefault((s.scene_id, s.group_id), []).append(s)
        for (_scene_id, gid), members in by_gid.items():
            members.sort(key=lambda x: x.order)
            g_total = sum(x.duration_sec for x in members)
            if len(members) < 2:
                issues.append(f"镜头组 {gid} 只有一个成员（应拆回单镜）")
            elif any(b.order != a.order + 1
                     for a, b in zip(members, members[1:], strict=False)):
                issues.append(f"镜头组 {gid} 的成员 order 不连续（组=一次生成，必须相邻）")
            elif not (GROUP_MIN_SEC <= g_total <= GROUP_MAX_SEC):
                issues.append(
                    f"镜头组 {gid} 总时长 {g_total:.0f}s 超出一次生成区间 "
                    f"[{GROUP_MIN_SEC:.0f}, {GROUP_MAX_SEC:.0f}]s")
            elif [x.group_index for x in members] != list(range(len(members))):
                issues.append(f"镜头组 {gid} 的 group_index 不是 0..n-1 连续序号")

        for bd in breakdowns:
            scene_shots = sorted(bd.shots, key=lambda s: s.order)
            scene = scene_of.get(bd.scene_id)
            scene_beats = scene.beats if scene else []
            # 按 dialogue_ref 的分界点把台词切到各镜（不是按「整段对话」分，
            # 否则一段甲乙对话会把两个人的台词都算进每一镜）
            shots_dialogue = assign_dialogue_ranges(scene_shots, scene_beats)
            groups = {s.reference_group for s in scene_shots if s.reference_group}
            if len(scene_shots) > 1 and not groups:
                issues.append(
                    f"场景 {bd.scene_id} 有 {len(scene_shots)} 个镜头但都没有 reference_group，"
                    "请给同场景镜头统一分组（硬切新地点可另起分组）以共享环境参考图"
                )
            # 6d) 每句台词都必须落在某个镜头的范围里，否则配音时无处安放
            dialogue_lines = [b for b in scene_beats if b.type == "dialogue" and (b.line or "").strip()]
            assigned = {b.id for beats in shots_dialogue.values() for b in beats}
            missing = [b for b in dialogue_lines if b.id not in assigned]
            if missing:
                preview = "、".join(f"「{(b.line or '').strip()[:10]}」" for b in missing[:2])
                issues.append(
                    f"场景 {bd.scene_id} 里有 {len(missing)} 句台词没有任何镜头认领"
                    f"（{preview} 等）：每句台词都要落在某个镜头的 dialogue_ref 范围里，"
                    "否则配音时它无处安放，会落到画面完全不相关的镜头上，观众听到声音却看不到人在说"
                )
            # 6e) dialogue_ref 必须指向本场真实存在的对白节拍
            # 这一条过去只查正向（台词有没有镜头认领），没查反向（镜头标的 ref 是不是真的），
            # 所以模型能一路绿灯地编 id。配音按 ref 找台词找不到 → 那句台词凭空消失，
            # 或者落到别的镜头上——观众听到声音却看不到人在说。
            dialogue_ids = {b.id for b in dialogue_lines}
            beat_type = {b.id: b.type for b in scene_beats}
            for s in scene_shots:
                ref = str(s.dialogue_ref or "").strip()
                if not ref or ref in dialogue_ids:
                    continue
                if ref in beat_type:
                    issues.append(
                        f"镜头 {s.id} 的 dialogue_ref={ref} 指向的是「{beat_type[ref]}」节拍，"
                        "不是对白节拍：dialogue_ref 只能填本场 [对白 beat_xxx] 里的 id"
                    )
                else:
                    issues.append(
                        f"镜头 {s.id} 的 dialogue_ref={ref} 在剧本里不存在：只能照抄本场"
                        "「节拍」清单里 [对白 beat_xxx] 的 id，不要自己拼新 id"
                        "（拼出来的 id 找不到台词，配音会静默跳过）"
                    )
            prev = None
            for s in scene_shots:
                # 3) 时长：生成单位是「片段」（组），不是单个分镜
                #    2026-11-20 起分镜可以短到 2 秒（一个小事件的一个机位），
                #    但**独立成片段的分镜**仍必须落在 4–15 秒（厂商下限 4 秒）。
                if not s.group_id and (s.duration_sec < 4.0 or s.duration_sec > 15.0):
                    issues.append(
                        f"镜头 {s.id} 的时长 {s.duration_sec}s 超出生成器可行区间（4–15 秒），请调整"
                    )
                # 6b) 动作密度：过载不再打回，由代码拆分（2026-11-20）
                #     旧口径是「段数 > 时长÷3 → 打回让模型重写」。实测那是治标：
                #     模型照旧写长（教了不做），而打回要烧一整轮导演调用。
                #     现在过载的分镜由 split_action_chains 就地拆成相邻分镜，
                #     所以这里不再报问题（报了也是白烧一轮）。
                # 6b-) 动作下限：只拦「短 + 全是微动作」这一种——
                #     静态机位 + 一个微动作 = 模型把它循环播放（2026-11-12 v3
                #     实测「83 秒重复一个动作」）。新粒度下一个分镜本就可以
                #     只写一件事，所以不再要求「≥3 段」，只要求「有一段位移级
                #     大动作」——除非它短到只有一两句。
                phrases = count_action_phrases(s.action)
                if phrases <= 2 and not _MOVEMENT_RE.search(s.action or ""):
                    issues.append(
                        f"镜头 {s.id} 的 action 只有 {phrases} 句且全是面部/手指微动："
                        "视频模型会把它循环播放，观众看到的就是 PPT。给这一镜一条"
                        "动作弧线：起（位移级动作，如起身/绕过柜台/俯身去拿）→"
                        "过（接触或反应）→ 落（final_frame 的结束态）。"
                        "情绪要翻译成位移：不是「他在生气」，是「他把碗顿在柜台上，半碗酒溅出来」。"
                    )
                # 6c) 一镜一说话人 + 台词要念得完
                seg = shots_dialogue.get(s.id) or []
                if seg:
                    speakers: list[str] = []
                    for b in seg:
                        name = char_of.get(b.speaker or "", b.speaker or "")
                        if (b.line or "").strip() and name and name not in speakers:
                            speakers.append(name)
                    if len(speakers) > 1:
                        issues.append(
                            f"镜头 {s.id} 里挤了 {len(speakers)} 个说话人（{'、'.join(speakers)}）："
                            "AI 生成没有口型同步，多人同镜说话，观众根本分不清是谁在说。"
                            "请按说话人拆镜（一人一镜），或让旁人只做身体反应、不说话"
                        )
                    chars = sum(len((b.line or "").strip()) for b in seg)
                    need = dialogue_need_sec(seg)
                    if chars and s.duration_sec < need:
                        issues.append(
                            f"镜头 {s.id} 有 {chars} 字台词，按每秒 {CHARS_PER_SEC:.0f} 字至少要 "
                            f"{need:.1f} 秒才念得完，当前只有 {s.duration_sec:.0f} 秒："
                            "请延长时长或把这句台词移到别的镜头"
                        )
                # 6) 运动镜头必须有运镜轨迹（固定机位不需要）
                if s.camera.type in _MOVING_CAMERA_TYPES and not s.camera.path.strip():
                    issues.append(
                        f"镜头 {s.id} 用了 {s.camera.type} 运镜却没有写 camera_path："
                        "请写清镜头从哪出发、到哪停下、相对主体的距离与机位高度怎么变，"
                        "写不出来就改成 static"
                    )
                # 4) 非首镜要说明为什么必须切
                if prev is not None and not str(s.cut_reason or "").strip():
                    issues.append(
                        f"镜头 {s.id} 没有填写 cut_reason：只有时间跳变/地点切换/无可回避的戏剧切口才允许切镜，"
                        "否则请合并进上一个镜头"
                    )
                # 5) 相邻重复的同一主体特写
                if prev is not None and s.subject.strip() and s.subject.strip() == str(prev.subject).strip():
                    issues.append(
                        f"镜头 {s.id} 与上一镜是同一主体（{s.subject}）的重复特写，"
                        "除非有强戏剧理由，否则合并成一个镜头"
                    )
                # 2) 接力只能指向更早的镜头
                if s.chain_from is not None:
                    if s.chain_from not in all_orders:
                        issues.append(f"镜头 {s.id} 的 chain_from={s.chain_from} 指向不存在的镜头")
                    elif s.chain_from >= s.order:
                        issues.append(f"镜头 {s.id} 的 chain_from={s.chain_from} 不是前序镜头，首帧无法接力（只能链到更早的镜头）")
                prev = s
        # 7-10) 戏剧结构审计：三工作 / 最终画面 / 三细节 / 五锚点（先戏剧，后语法）
        issues.extend(audit_breakdowns(breakdowns))
        return issues

    def _build_prompt(
        self,
        script: Script,
        targets: list,
        adaptation_type: str | None = None,
        visual_style: str | None = None,
    ) -> str:
        char_map = {c.id: c.name for c in script.characters}
        loc_map = {loc.id: loc.name for loc in script.locations}

        parts = [
            f"剧本：《{script.title}》",
            f"梗概：{script.logline}",
            f"主题：{'、'.join(script.themes)}",
            f"人物：{'、'.join(c.name + '(' + (c.role or '未定') + ')' for c in script.characters)}",
        ]
        parts.extend(_format_constraints(
            adaptation_type, visual_style,
            batch_scenes=len(targets), total_scenes=max(1, len(script.scenes)),
        ))
        parts.extend(["", "请为以下场景设计镜头方案："])

        for sc in targets:
            loc_name = loc_map.get(sc.location_id, sc.location_id)
            char_names = "、".join(char_map.get(cid, cid) for cid in sc.characters)
            parts.append(f"\n--- {sc.id} {sc.title} ---")
            parts.append(f"地点：{loc_name}　时间：{sc.time or '未定'}")
            parts.append(f"人物：{char_names}")
            parts.append(f"目的：{sc.purpose}")
            parts.append(f"冲突：{sc.conflict}")

            if sc.entry_state:
                parts.append(f"入口状态：{sc.entry_state}")
            if sc.exit_state:
                parts.append(f"出口状态：{sc.exit_state}")

            parts.append("节拍：")
            for b in sc.beats:
                # **必须带 beat id**：dialogue_ref 要引用它。不带 id 时模型只能自己拼一个
                # （实测 35 个引用里 28 个是编的，如 beat_003_dlg_001），配音照常配、
                # 画面照常生成，两条线永不交汇——「台词硬塞」的根源就在这里。
                if b.type == "action":
                    parts.append(f"  [动作 {b.id}] {b.text}")
                elif b.type == "dialogue":
                    name = char_map.get(b.speaker, b.speaker) if b.speaker else "?"
                    emo = f"（{b.emotion}）" if b.emotion else ""
                    parts.append(f"  [对白 {b.id}] {name}{emo}：{b.line}")
                elif b.type == "cue":
                    parts.append(f"  [提示 {b.id}] {b.text}")

        return "\n".join(parts)

    def _parse_breakdowns(
        self, raw: Any, *, default_pacing: str = "medium", start_order: int = 0,
    ) -> list[SceneBreakdown]:
        """将 LLM JSON 输出解析为 SceneBreakdown 列表。

        模型给的是「场景内局部 order」（每场都从 0 开始），而接力/参考逻辑需要
        全片唯一的全局 order。这里统一重编号，并把 chain_from 从局部编号映射到全局。

        ``start_order``：分批拆解时，这一批镜头在整部片里的起始编号——否则每批
        都从 0 开始，合并后会出现大量重复 order。
        """
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raise ValueError(f"期望数组，得到 {type(raw)}")

        breakdowns: list[SceneBreakdown] = []
        next_order = start_order
        for item in raw:
            scene_id = item.get("scene_id", "")
            shots_in = sorted(item.get("shots", []), key=lambda x: x.get("order", 0))

            # 第一遍：局部 order -> 全局 order
            local_to_global: dict[Any, int] = {}
            for idx, s in enumerate(shots_in):
                local_to_global[s.get("order", idx)] = next_order
                next_order += 1

            # 第二遍：构造 Shot，chain_from 映射到全局编号
            shots: list[Shot] = []
            for idx, s in enumerate(shots_in):
                direction = str(s.get("camera_direction") or "").strip() or None
                cam = CameraMovement(
                    type=_pick_enum(s.get("camera_type"), _CAMERA_TYPES, _CAMERA_ALIASES, "static"),
                    direction=direction,
                    speed=_pick_enum(s.get("camera_speed"), _CAMERA_SPEEDS, {}, "medium"),
                    path=str(s.get("camera_path") or "").strip(),
                    height=str(s.get("camera_height") or "").strip(),
                )
                g_order = local_to_global[s.get("order", idx)]
                chain = self._coerce_chain(s.get("chain_from"))
                if chain is not None:
                    # 模型在场景内用局部 order 接力：映射到全局；映射不到则丢弃。
                    chain = local_to_global.get(chain)
                shot = Shot(
                    id=f"shot_{scene_id or 'scene_000'}_{g_order:03d}",
                    scene_id=scene_id,
                    order=g_order,
                    shot_type=_pick_enum(s.get("shot_type"), _SHOT_TYPES, _SHOT_ALIASES, "medium"),
                    camera=cam,
                    subject=s.get("subject", ""),
                    action=s.get("action", ""),
                    dialogue_ref=s.get("dialogue_ref"),
                    # 台词在镜头内第几秒开口：让配音跟画面上正在发生的动作对齐。
                    dialogue_at=_coerce_float(s.get("dialogue_at")),
                    # 模型偶尔给出 0.8 这类非法时长，钳制到 Shot 允许区间。
                    duration_sec=min(30.0, max(1.0, float(s.get("duration_sec") or 5.0))),
                    lighting=s.get("lighting"),
                    mood=s.get("mood"),
                    spatial=str(s.get("spatial") or "").strip(),
                    background_action=str(s.get("background_action") or "").strip(),
                    cut_reason=str(s.get("cut_reason") or ""),
                    # 三工作规则：非法值一律置空，交给校验环节批评重拆。
                    function=str(s.get("function") or "").strip() if str(s.get("function") or "").strip() in SHOT_FUNCTIONS else "",
                    final_frame=str(s.get("final_frame") or "").strip(),
                    reference_group=str(s.get("reference_group") or ""),
                    chain_from=chain,
                    # 镜头组（D-46）：模型给同一戏剧高点的相邻机位标同一个
                    # group 键；此处只宽松收字符串，合法性（连续/时长/序号）
                    # 由 _normalize_groups 归一化 + 校验器把关。
                    group_id=str(s.get("group") or "").strip(),
                    # 占位序号：_normalize_groups 会按组重排 0..n-1，这里钳到合法区间
                    group_index=min(idx, 5) if str(s.get("group") or "").strip() else 0,
                )
                shots.append(shot)

            anchors_raw = item.get("anchors")
            if isinstance(anchors_raw, SceneAnchors):
                anchors = anchors_raw
            elif isinstance(anchors_raw, dict):
                # 模型可能多给键/给错类型：只收五锚点字段，宽松收场。
                anchors = SceneAnchors(
                    **{k: str(v or "") for k, v in anchors_raw.items() if k in SceneAnchors.model_fields}
                )
            else:
                anchors = SceneAnchors()
            breakdown = SceneBreakdown(
                scene_id=scene_id,
                director_notes=item.get("director_notes", ""),
                visual_approach=item.get("visual_approach", ""),
                pacing=_pick_enum(item.get("pacing"), _PACINGS, {}, default_pacing),
                key_moments=item.get("key_moments", []),
                anchors=anchors,
                shots=shots,
            )
            breakdowns.append(breakdown)

        # 动作链过长的分镜拆成相邻分镜（一个分镜 = 一个小事件）。
        # 拆完要重排 order / id / chain_from：拆出来的兄弟分镜共用同一个旧
        # order，order 是 id 的一部分，不重排会出现重复 id。
        split_action_chains(breakdowns)
        next_order = _renumber_shots(breakdowns, start_order)

        _normalize_groups(breakdowns)
        # 导演不标组时由代码兜底成组（同场相邻孤立单镜，≤15s/≤4 员）。
        auto_group_scene(breakdowns)
        return breakdowns


    @staticmethod
    def _coerce_chain(value: Any) -> int | None:
        """把模型给的 chain_from 规整为 int 或 None。"""
        if value is None or value == "" or str(value).strip().lower() in {"null", "none"}:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    def _fallback(self, script: Script, scene_ids: list[str] | None) -> CrewTaskResult:
        """无模型时的回退：为每个场景生成最少 1 个镜头。"""
        targets = script.scenes
        if scene_ids:
            targets = [sc for sc in script.scenes if sc.id in scene_ids]

        breakdowns: list[SceneBreakdown] = []
        for sc in targets:
            shot = Shot(
                id=f"shot_{sc.id}_001",
                scene_id=sc.id,
                order=0,
                shot_type="medium",
                camera=CameraMovement(type="static"),
                subject=sc.title,
                action=sc.purpose,
                duration_sec=5.0,
                reference_group=sc.id,  # 单镜场景也挂一个分组，便于后续扩展
            )
            breakdown = SceneBreakdown(
                scene_id=sc.id,
                director_notes="（无模型回退：默认中景固定镜头）",
                pacing="medium",
                shots=[shot],
            )
            breakdowns.append(breakdown)

        assign_episodes(breakdowns, script)
        return CrewTaskResult(
            data=breakdowns,
            summary=f"（无模型）为 {len(breakdowns)} 个场景生成默认镜头方案",
        )


def assign_episodes(breakdowns: list[SceneBreakdown], script: Script) -> None:
    """给每个镜头标上「它属于第几集」（D-63 集-镜两级结构）。

    集 = 小说的一章：场景本来就带着 chapter_refs（来自原著章节），
    那一章就是它所属的集。连载改编时一章一集。

    **一章装不下一集时按时长拆集**（2026-11-20《替身v2》复盘新增）：
    内容比例预算（每场保底 24 秒）让单章短作的总时长可以超过一集
    （10 场 ≈ 240 秒 = 两集）。旧版全塞一集，观众看到的就是 4 分钟
    「一集」——即梦对同一篇原文拆 5 集 × 60–95 秒才是短剧的集形态。
    拆法：章内按场次顺序贪心装箱，每集合计 ≤ EPISODE_TARGET_SEC；
    单场超一集时长的不硬拆（场是戏剧单元），它自己占一集。
    集 id 加 `#n` 后缀（`ch_001#2`），episode_index 全局连续。

    就地写（与 group 归一同样的做法）：不新增一张表，不改镜头方案的结构，
    只在镜头上多两个字段。
    """
    from ..video.rhythm import EPISODE_TARGET_SEC

    order: dict[str, int] = {}
    for i, ch in enumerate(script.source.chapter_ids or []):
        order.setdefault(ch, i)
    scene_ch: dict[str, str] = {}
    for sc in script.scenes:
        ch = sc.chapter_refs[0] if sc.chapter_refs else sc.id
        if ch not in order:
            order[ch] = len(order)
        scene_ch[sc.id] = ch

    # 每场的实际分镜时长（闸门后），供贪心装箱
    scene_dur: dict[str, float] = {}
    for bd in breakdowns:
        scene_dur[bd.scene_id] = sum(
            max(0.0, s.duration_sec) for s in bd.shots)

    # 按章分组（保持场次顺序），超一集的章拆成多集
    by_chapter: dict[str, list[str]] = {}
    for bd in breakdowns:
        ch = scene_ch.get(bd.scene_id, bd.scene_id)
        by_chapter.setdefault(ch, []).append(bd.scene_id)

    ep_of_scene: dict[str, tuple[str, int]] = {}
    ep_counter = 0
    for ch in sorted(by_chapter, key=lambda c: order.get(c, 0)):
        scenes_in_ch = by_chapter[ch]
        # 贪心装箱：累计 ≤ EPISODE_TARGET_SEC 就并进当前集
        parts: list[list[str]] = []
        run: list[str] = []
        run_sec = 0.0
        for sid in scenes_in_ch:
            d = scene_dur.get(sid, 0.0)
            if run and run_sec + d > EPISODE_TARGET_SEC:
                parts.append(run)
                run, run_sec = [], 0.0
            run.append(sid)
            run_sec += d
        if run:
            parts.append(run)
        for pi, part in enumerate(parts):
            ep_id = ch if len(parts) == 1 else f"{ch}#{pi + 1}"
            for sid in part:
                ep_of_scene[sid] = (ep_id, ep_counter)
            ep_counter += 1

    for bd in breakdowns:
        ep_id, ep_idx = ep_of_scene.get(bd.scene_id, (bd.scene_id, 0))
        for s in bd.shots:
            s.episode_id = ep_id
            s.episode_index = ep_idx


def run_director(
    llm: LLM,
    script: Script,
    *,
    scene_ids: list[str] | None = None,
    adaptation_type: str | None = None,
    visual_style: str | None = None,
) -> CrewTaskResult:
    """快捷入口：执行导演场景拆解。"""
    agent = DirectorAgent(llm)
    return agent.run(
        script, scene_ids=scene_ids,
        adaptation_type=adaptation_type, visual_style=visual_style,
    )
