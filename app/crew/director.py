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
    """单镜能演清楚的「身体动作段」上限。

    注意这不是「几个动作」而是「几个分句」：模型写一个动作时习惯补两笔细节
    （「把碗搁在柜台上；下巴一抬；手指点过去」其实是同一个动作的三步），
    所以下限给 3 段；越长的镜头允许写越多笔。

    至于「一镜里多个主体各做各的动作」，试过用角色名出现次数来判，但正则分不清
    「甲指着乙」和「乙在做动作」，实测全是误报（还把合格的分镜改坏），已撤回——
    这一条交给导演规则本身约束，机械层只兜底「明显塞太满」。
    """
    return max(3, round(duration_sec / 3))


def count_action_units(action: str) -> int:
    """数一个镜头里写了几段身体动作（按分号/换行切，过短的碎片不计）。"""
    parts = re.split(r"[；;\n]+", action or "")
    return len([p for p in parts if len(p.strip()) >= 4])


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


def _budget_for(adaptation_type: str | None) -> float:
    """成片目标时长（秒）。短剧 120 秒。"""
    from ..video.rhythm import duration_budget
    return duration_budget(adaptation_type)


# 确定性闸门用的单镜时长上限：短剧节奏基线（导演技能「不要写 8 秒以上」）。
CAP_SHOT_SEC = 6.0


def enforce_duration_budget(
    breakdowns: list[SceneBreakdown], budget_sec: float,
) -> tuple[list[SceneBreakdown], list[str]]:
    """确定性片长闸门：导演回喂多轮仍超预算时，代码直接砍。

    为什么必须有这一道：预算在提示词里写了、校验里也报了，但那些都只是「回喂批评」——
    模型可以不理，迭代用完照样放行（2026-11-12 孔乙己三次实测：45 / 39 / 53 镜，
    全部超 120 秒预算一倍以上）。超预算不只是节奏问题：每一镜都是一次真金白银的
    视频生成，53 镜意味着近三倍的算力与账户余额。批评管不住的数，代码来管。

    砍法（先便宜后疼）：
      1. 单镜时长封顶 CAP_SHOT_SEC——长镜头先瘦身；
      2. 还超就删镜头，优先删「壁纸镜头」（function 不在三工作里），
         再删没认领台词的，同样条件删时长最长的；带台词的镜头最后才动
         （删了会丢配音，宁可多留）；
      3. 删完修复 chain_from 悬空引用：指向被删 order 的改指向最近的保留前镜，
         没有前镜可指就断开接力（None）。
    返回 (压缩后的分镜, 人话说明列表)。调用方负责把说明拼进 summary。
    """
    notes: list[str] = []
    all_shots = [s for bd in breakdowns for s in bd.shots]
    if not all_shots:
        return breakdowns, notes

    total = sum(max(0.0, s.duration_sec) for s in all_shots)
    if total <= budget_sec:
        return breakdowns, notes

    # 1) 时长封顶
    capped = 0
    for s in all_shots:
        if s.duration_sec > CAP_SHOT_SEC:
            s.duration_sec = CAP_SHOT_SEC
            capped += 1
    total = sum(max(0.0, s.duration_sec) for s in all_shots)
    if capped:
        notes.append(f"闸门：{capped} 个超长镜头已封顶 {CAP_SHOT_SEC:.0f} 秒")

    # 2) 删镜头直到进预算。给每个镜头打「该先删」分：越小越先删。
    def drop_priority(s: Shot) -> tuple:
        wallpaper = 0 if s.function not in SHOT_FUNCTIONS else 1
        no_dialogue = 0 if not (s.dialogue_ref or "").strip() else 1
        # 时长长的先删（省得多）；同分按 order 稳定
        return (wallpaper, no_dialogue, -s.duration_sec, s.order)

    kept = list(all_shots)
    dropped: list[Shot] = []
    while sum(max(0.0, s.duration_sec) for s in kept) > budget_sec and len(kept) > 1:
        victim = min(kept, key=drop_priority)
        kept.remove(victim)
        dropped.append(victim)

    if dropped:
        notes.append(
            f"闸门：仍超预算，删掉 {len(dropped)} 个镜头"
            f"（壁纸 {sum(1 for d in dropped if d.function not in SHOT_FUNCTIONS)} 个、"
            f"无台词 {sum(1 for d in dropped if not (d.dialogue_ref or '').strip())} 个），"
            f"压到 {sum(s.duration_sec for s in kept):.0f} 秒"
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
    from ..video.rhythm import duration_budget, normalize_format, rhythm_table

    fmt = normalize_format(adaptation_type)
    table = rhythm_table(fmt)
    spec = get_visual_style(visual_style)
    budget = duration_budget(fmt)

    lines = [""]
    if fmt == "short_drama":
        # 分批拆解时，模型只看得到自己这一批的戏——「整片 120 秒」对它没有约束力，
        # 它会照剧情一路拆细（实测：说了 120 秒，仍拆出 62 镜 300+ 秒）。所以要把
        # 预算按场数折算成「这一批只有多少秒」，它才知道该合并多少。
        share = budget * max(1, batch_scenes) / max(1, total_scenes)
        lines += [
            "【类型约束】短剧：横屏 16:9（默认，工作台可切竖屏）、1–2 分钟、前 3 秒必须抓住注意力。",
            f"【本批预算】这次要拆 {batch_scenes} 场，合计约 **{share:.0f} 秒、"
            f"{max(1, int(share / table['medium']))}–{max(1, int(share / table['fast']))} 个镜头**"
            f"（全片 {total_scenes} 场共约 {budget:.0f} 秒）。超出这个数就是超了，"
            "要合并镜头，不是加镜头。",
            "不是「把小说拍完」，是「把这场冲突拍爽」——过场、铺垫、次要人物的反应镜头，"
            "该合并就合并、该删就删。",
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


class DirectorAgent(CrewAgent):
    """导演 Agent：将剧本拆解为镜头方案。"""

    name = "director"
    label = "导演"
    emoji = "🎬"

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
                    return CrewTaskResult(
                        success=False,
                        summary=f"导演工作失败：{'、'.join(sc.id for sc in batch)} 拆解失败",
                        errors=critique,
                    )
                breakdowns.extend(bds)
            last = breakdowns
            critique = self._validate_continuity(
                breakdowns, script,
                budget_sec=_budget_for(adaptation_type),
            )
            if not critique:
                # 校验通过（在 1.5x 上限内）也要过确定性闸门：
                # 闸门按「预算」而不是「上限」收——超预算的秒数就是超预算的钱。
                budget = _budget_for(adaptation_type)
                gated, notes = enforce_duration_budget(breakdowns, budget)
                summary = (
                    f"导演完成 {len(gated)} 个场景的镜头拆解，"
                    f"共 {sum(len(b.shots) for b in gated)} 个镜头"
                )
                if notes:
                    summary += "；" + "；".join(notes)
                return CrewTaskResult(data=gated, summary=summary)

        # 迭代用完仍不合格：批评可以不理，预算不能不理——钱是硬约束。
        budget = _budget_for(adaptation_type)
        gated, notes = enforce_duration_budget(last, budget)
        total = sum(len(b.shots) for b in gated) if gated else 0
        summary = f"导演完成拆解（共 {total} 镜）"
        if notes:
            summary += "；" + "；".join(notes)
        if critique:
            summary += f"，但连续性校验仍待完善：{critique[0]}"
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
            log.warning("导演拆解失败（%s）：%s", "、".join(sc.id for sc in targets), e)
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

        # 0) 片长预算：短剧是 1–2 分钟的片子，不是「把小说拍完」。
        #    没有这一条时，导演会按剧情一路拆下去——《孔乙己》七场戏拆出 60 镜
        #    295 秒，是短剧的三倍，生成成本与观众耐心都不允许。
        if budget_sec:
            total = sum(max(0.0, s.duration_sec) for bd in breakdowns for s in bd.shots)
            limit = budget_sec * 1.5
            if total > limit:
                n = sum(len(bd.shots) for bd in breakdowns)
                issues.append(
                    f"整片总时长 {total:.0f} 秒、共 {n} 个镜头，超出预算（目标 {budget_sec:.0f} 秒，"
                    f"上限 {limit:.0f} 秒）。短剧不是「把小说拍完」：请合并同场景的连续动作、"
                    "删掉过场与铺垫镜头、把次要人物的反应镜头并进主镜，"
                    f"压到 {int(budget_sec / 5)} 个镜头左右。"
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
                # 3) 时长必须在模型可行区间内
                if s.duration_sec < 4.0 or s.duration_sec > 15.0:
                    issues.append(
                        f"镜头 {s.id} 的时长 {s.duration_sec}s 超出生成器可行区间（4–15 秒），请调整"
                    )
                # 6b) 动作密度：一镜演不下这么多身体动作
                units = count_action_units(s.action)
                budget = action_budget(s.duration_sec)
                if units > budget:
                    issues.append(
                        f"镜头 {s.id} 的 action 写了 {units} 段身体动作，{s.duration_sec:.0f} 秒只演得下 "
                        f"{budget} 段：只保留这一镜的主事件（接触 → 完成，至多两步），"
                        "其余的删掉或另起一镜。动作塞太满时模型会丢节拍、挪时序，"
                        "人物就出现在不该在的位置，手也会穿模"
                    )
                # 6b-) 动作下限（2026-11-12 v3 实测：24 镜 22 镜只写 2 段微动作，
                #     静态机位 + 微动作 = 模型把动作循环播放，整片「83 秒重复一个动作」）：
                #     段数至少 3，且至少一段是位移级大动作（纯计数与词表，客观可查）。
                if units < 3:
                    issues.append(
                        f"镜头 {s.id} 的 action 只有 {units} 段身体动作（至少 3 段）："
                        "4 秒静态画面只写一个微动作，视频模型会把它循环播放，"
                        "观众看到的就是 PPT。给这一镜一条动作弧线：起（位移级动作，"
                        "如起身/绕过柜台/俯身去拿）→ 过（接触或反应）→ 落（final_frame 的结束态）。"
                        "情绪要翻译成位移：不是「他在生气」，是「他把碗顿在柜台上，半碗酒溅出来」。"
                    )
                elif not _MOVEMENT_RE.search(s.action or ""):
                    issues.append(
                        f"镜头 {s.id} 的 action 三段全是面部/手指微动（没有一段位移级大动作）："
                        "至少一段要让整个身体动起来（起身、跨半步、绕过柜台、俯身去拿、退回去坐下），"
                        "微动作留给其余两段做节拍。"
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
                    need = chars / CHARS_PER_SEC + 0.2
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

        return CrewTaskResult(
            data=breakdowns,
            summary=f"（无模型）为 {len(breakdowns)} 个场景生成默认镜头方案",
        )


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
