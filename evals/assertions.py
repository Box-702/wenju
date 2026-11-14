# =====================================================================
# assertions.py —— 提示词质量的确定性断言库
#
# 这些断言是 eval 的「硬层」：不依赖 LLM 判定，只跑规则，因此
# **每次都稳定、零成本、可进 CI**。它们检查的是「一定会导致出片失败」
# 的硬伤，而不是「写得好不好」——后者交给 LLM judge（见 judge_prompt.md）。
#
# 判定分两级：
#   critical —— 违反就是明确缺陷，必须修（CI 里直接失败）
#   warn     —— 违反是质量风险，不一定错（CI 里只报告）
#
# 复用 app/crew/dramaturgy.py 的检测器，不重复造词表：
# 审计规则改一次，这里自动跟着变。
# =====================================================================

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.crew.director import count_action_units
from app.crew.dramaturgy import (
    SHOT_FUNCTIONS,
    audit_breakdowns,
    find_camera_moves,
    find_negations,
    find_repeated_clause,
    find_slop_words,
    find_spoken_quotes,
    strip_style_block,
    three_details,
)
from app.domain import Script, SceneBreakdown, Shot, StyleGuide

# 目标视频模型的可行时长区间（秒）。超出区间的镜头多半会被服务商忽略或失败。
MIN_SHOT_SEC = 4.0
MAX_SHOT_SEC = 15.0


@dataclass
class Failure:
    """一条断言失败记录。"""

    rule: str  # 规则 id，如 "DPT-02"
    severity: str  # critical / warn
    where: str  # 定位，如 "scene_001/shot_002"
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule} @ {self.where}: {self.message}"


@dataclass
class AssertionResult:
    """一个被评对象的断言结果。"""

    target: str  # "director" / "dp"
    failures: list[Failure] = field(default_factory=list)

    @property
    def critical(self) -> list[Failure]:
        return [f for f in self.failures if f.severity == "critical"]

    @property
    def warns(self) -> list[Failure]:
        return [f for f in self.failures if f.severity == "warn"]

    @property
    def passed(self) -> bool:
        return not self.critical

    def add(self, rule: str, severity: str, where: str, message: str) -> None:
        self.failures.append(Failure(rule=rule, severity=severity, where=where, message=message))


# ---------------------------------------------------------------------
# 一、导演侧断言
# ---------------------------------------------------------------------


def assert_director(
    breakdowns: list[SceneBreakdown],
    *,
    script: Script | None = None,
) -> AssertionResult:
    """检查导演分镜产出。

    覆盖：每镜是否做功（三工作规则）、是否有最终画面、细节密度、
    切镜理由、时长可行性、接力链路自洽、场景锚点是否命名。
    """
    res = AssertionResult(target="director")
    if not breakdowns:
        res.add("DIR-00", "critical", "director", "导演没有产出任何分镜")
        return res

    # 五锚点 / function / final_frame / 细节 —— 复用生产代码里的审计器，
    # 保证 eval 与线上审计用的是同一套规则（否则 eval 通过了线上照样挂）。
    for issue in audit_breakdowns(breakdowns):
        res.add("DIR-01", "critical", "director", issue)

    seen_orders: set[int] = set()
    for bd in breakdowns:
        where_scene = bd.scene_id
        for s in bd.shots:
            where = f"{where_scene}/{s.id}"

            # function 必须合法（audit_breakdowns 已查，这里兜底防重复实现漂移）
            if s.function and s.function not in SHOT_FUNCTIONS:
                res.add(
                    "DIR-02", "critical", where,
                    f"function 取值非法：{s.function!r}（合法值：{'/'.join(SHOT_FUNCTIONS)}）",
                )

            # 时长可行性
            if not (MIN_SHOT_SEC <= s.duration_sec <= MAX_SHOT_SEC):
                res.add(
                    "DIR-03", "warn", where,
                    f"镜头时长 {s.duration_sec}s 超出目标模型可行区间 "
                    f"[{MIN_SHOT_SEC}, {MAX_SHOT_SEC}]s，多半会被忽略或失败",
                )

            # 接力链路自洽：chain_from 必须指向已存在的、更早的 order
            if s.chain_from is not None:
                if s.chain_from not in seen_orders:
                    res.add(
                        "DIR-04", "critical", where,
                        f"chain_from={s.chain_from} 指向一个不存在或更晚的镜头（接力会错位）",
                    )
                if s.chain_from >= s.order:
                    res.add(
                        "DIR-05", "critical", where,
                        f"chain_from={s.chain_from} 不小于自身 order={s.order}（不能接自己的未来）",
                    )

            # 主体不能为空——空主体的镜头渲染不出东西
            if not s.subject.strip():
                res.add("DIR-06", "critical", where, "镜头没有主体（subject 为空）")

            # 动作不能为空
            if not s.action.strip():
                res.add("DIR-07", "warn", where, "镜头没有写动作（action 为空）")

            seen_orders.add(s.order)

    # 镜头数：一场戏至少 1 镜；单场超过 12 镜多半是切碎
    for bd in breakdowns:
        n = len(bd.shots)
        if n == 0:
            res.add("DIR-08", "critical", bd.scene_id, "这场戏一个镜头都没有")
        elif n > 12:
            res.add(
                "DIR-09", "warn", bd.scene_id,
                f"这场戏切了 {n} 镜，过碎（切碎 = 不连贯 + 更贵），建议合并",
            )

    # —— 全片层：镜头语言与动作密度（2026-11-12 evals 重设计）——
    # 导演内部的回喂批评管不住这两件事（v2 实测 25/25 静态、v3 实测 22/24
    # 只写 2 段微动作），线上靠确定性闸门兜底；eval 独立查**最终产物**，
    # 不信任闸门也报——闸门改了阈值时这里是第二只眼。
    all_shots = [s for bd in breakdowns for s in bd.shots]
    n_all = len(all_shots)
    if n_all >= 6:
        n_close = sum(1 for s in all_shots if s.shot_type in ("close_up", "extreme_close_up"))
        n_static = sum(1 for s in all_shots if (s.camera.type or "static") in ("static", "steady", ""))
        n_wide = sum(1 for s in all_shots if s.shot_type in ("extreme_wide", "wide"))
        if n_close / n_all > 0.4:
            res.add(
                "DIR-10", "critical", "director",
                f"特写占比 {n_close}/{n_all}（>40%）：全片怼脸 = 畸变高发 + 观感疲劳",
            )
        if n_static / n_all > 0.75:
            res.add(
                "DIR-11", "critical", "director",
                f"固定机位占比 {n_static}/{n_all}（>75%）：镜头不动 = 摄影机不存在",
            )
        if n_wide == 0:
            res.add(
                "DIR-12", "critical", "director",
                "全片没有一个 wide：场景空间从未交代，观众不知道人在哪",
            )
        thin = [s for s in all_shots if count_action_units(s.action) < 3]
        if len(thin) > n_all * 0.3:
            res.add(
                "DIR-13", "critical", "director",
                f"{len(thin)}/{n_all} 镜的 action 不足 3 段（微动作循环 = PPT，"
                "v3『83 秒重复一个动作』的直接指标）",
            )
        # 运镜配给制（2026-11-14 用户看片：闸门机械补运动后「推近复读三次」
        # = 另一种刻意）：相邻两镜不用同一种运镜；同型运镜之间至少隔两镜。
        # 只查运动镜头（static/steady 无方向，天然豁免）——静止不是罪。
        ordered_shots = sorted(all_shots, key=lambda x: x.order)
        movers = [s for s in ordered_shots
                  if (s.camera.type or "static") not in ("static", "steady", "")]
        for a, b in zip(movers, movers[1:], strict=False):
            key_a = (a.camera.type, (a.camera.direction or "").strip())
            key_b = (b.camera.type, (b.camera.direction or "").strip())
            if key_a == key_b:
                gap = b.order - a.order - 1
                if gap < 2:
                    res.add(
                        "DIR-14", "warn", "director",
                        f"运镜复读：{a.id} 与 {b.id} 连续使用同一种运镜"
                        f"（{key_a[0]}{'/' + key_a[1] if key_a[1] else ''}，中间仅隔 "
                        f"{gap} 镜）——运镜是配给制，相邻镜头换型或改静止",
                    )

    return res


# ---------------------------------------------------------------------
# 二、摄影指导侧断言
# ---------------------------------------------------------------------


def assert_dp(
    shots: list[Shot],
    *,
    script: Script | None = None,
    style_guide: StyleGuide | None = None,
) -> AssertionResult:
    """检查摄影指导产出的最终 video_prompt。

    覆盖：可渲染性（长度）、空洞词、否定句、引用台词、一镜多运镜、
    以及「视觉锚逐字复用」这条贯穿全链路的核心原则。
    """
    res = AssertionResult(target="dp")
    if not shots:
        res.add("DPT-00", "critical", "dp", "摄影指导没有产出任何镜头")
        return res

    env_by_scene: dict[str, str] = {}
    char_by_scene: dict[str, set[str]] = {}
    if script is not None:
        loc_name = {loc.id: loc.name for loc in script.locations}
        char_name = {c.id: c.name for c in script.characters}
        for sc in script.scenes:
            env_by_scene[sc.id] = loc_name.get(sc.location_id, "")
            char_by_scene[sc.id] = {char_name.get(cid, cid) for cid in sc.characters}

    canon_env: dict[str, str] = dict(style_guide.environment_descriptions) if style_guide else {}
    canon_char: dict[str, str] = dict(style_guide.character_appearances) if style_guide else {}

    for s in shots:
        where = f"{s.scene_id}/{s.id}"
        prompt = (s.video_prompt or "").strip()

        if not prompt:
            res.add("DPT-01", "critical", where, "镜头没有 video_prompt（无法提交出片）")
            continue

        # 过短：没有可渲染的物理事实，模型只能自由发挥
        if len(prompt) < 20:
            res.add("DPT-02", "critical", where, f"提示词过短（{len(prompt)} 字），模型只能自由发挥")

        slop = find_slop_words(prompt)
        if slop:
            res.add(
                "DPT-03", "critical", where,
                f"含渲染不出来的占位词：{'、'.join(sorted(set(slop)))}",
            )

        negs = find_negations(prompt)
        if negs:
            uniq = "、".join(dict.fromkeys(negs))
            res.add(
                "DPT-04", "critical", where,
                f"含否定式描述（{uniq[:30]}）：视频模型会把被否定的动作画出来",
            )

        quotes = find_spoken_quotes(prompt)
        if quotes:
            res.add(
                "DPT-05", "critical", where,
                f"含引用台词（「{quotes[0][:14]}」）：旧写法（无冒号）会渲染成含混假语音；官方句式需在引号前加冒号",
            )

        # 一镜多运镜
        #
        # 与生产审计器同源：剥掉全片风格段（style_guide.camera_style），
        # 且只认「带镜头语境」的运镜动词——「拉出一道冷光带」不是 pull-out。
        # 这两条都是实测踩出来的：不剥风格段、不看语境，真实提示词会 4/4 全误报。
        own_text = strip_style_block(prompt, style_guide.camera_style if style_guide else "")
        moves = set(find_camera_moves(own_text))
        if s.camera.path:
            moves |= set(find_camera_moves(s.camera.path))
        if len(moves) >= 2:
            res.add(
                "DPT-06", "warn", where,
                f"疑似一镜多运镜（{'、'.join(sorted(moves)[:3])}）：一镜只保留一种主要运动",
            )

        # 三细节密度（用 prompt 全文兜底判定）
        count, missing = three_details(
            action=s.action,
            lighting=s.lighting or "",
            background_action=s.background_action,
            prompt=prompt,
        )
        if count == 0:
            res.add("DPT-07", "critical", where, "提示词里一个物理细节都没有（零细节 = 废镜头）")
        elif count == 1:
            res.add("DPT-08", "warn", where, f"只有「{missing[0]}」一类细节，画面会单薄")

        # 视觉锚逐字复用（贯穿全链路的第一原则）
        #
        # 判定用「整段锚是否原样出现」+ 相似度分级，而不是比对前几个字——
        # 只看开头几个字会把「锚被轻微改写」误判成「完全没复用」，
        # 而把轻微改写放过又看不出真实退化。实测 DP 偶尔会重写锚里的个别词。
        if style_guide is not None:
            env_name = env_by_scene.get(s.scene_id, "")
            canon_env_text = canon_env.get(env_name, "") if env_name else ""
            if canon_env_text:
                ratio = _anchor_similarity(canon_env_text, prompt)
                if canon_env_text not in prompt and ratio < 0.85:
                    res.add(
                        "DPT-09", "critical", where,
                        f"没有逐字复用环境锚「{env_name}」（相似度 {ratio:.0%}）："
                        "下游必须复用美术指导产出的锚，每镜重写会导致跨镜漂移",
                    )
                elif canon_env_text not in prompt:
                    res.add(
                        "DPT-11", "warn", where,
                        f"环境锚「{env_name}」被轻微改写（相似度 {ratio:.0%}）："
                        "锚应原样复用，改写会让同一地点在不同镜头里长得不一样",
                    )
            for cid in sorted(char_by_scene.get(s.scene_id, set())):
                appear = canon_char.get(cid, "")
                if not appear:
                    continue
                # 只查**真的出现在这一镜里**的角色：场景人物表里的人在某一镜可能根本不出场，
                # 没出场自然不该有锚。判据是「提示词里点了他的名」。
                if cid not in prompt:
                    continue
                ratio = _anchor_similarity(appear, prompt)
                if appear not in prompt and ratio < 0.85:
                    res.add(
                        "DPT-10", "critical", where,
                        f"没有复用人物锚「{cid}」的造型（相似度 {ratio:.0%}）："
                        "逐字复用可避免跨镜长相漂移",
                    )
                elif appear not in prompt:
                    res.add(
                        "DPT-12", "warn", where,
                        f"人物锚「{cid}」被轻微改写（相似度 {ratio:.0%}）：锚应原样复用",
                    )

        # 光影与分镜的一致性。
        #
        # judge 基线（2026-10-30）里最扎眼的一类：分镜写「右侧窗户午后斜阳」，
        # 提示词改成「床头小夜灯」；下一镜又变成「右下方夜灯」。单个镜头看都通顺，
        # 连起来就是同一场戏忽明忽暗、人物脸光乱跳。光源是分镜给的硬信息，
        # 模型只该补方向与相对关系，不该换掉它。
        if s.lighting:
            want_src, want_side = _light_profile(s.lighting)
            got_src, got_side = _light_profile(own_text)
            if want_src and got_src and not (want_src & got_src):
                res.add(
                    "DPT-13", "warn", where,
                    f"光源被换掉（分镜写「{'、'.join(sorted(want_src))}」，"
                    f"提示词写「{'、'.join(sorted(got_src))}」）："
                    "同一场的光源变了，跨镜脸光会跳",
                )
            elif want_side and got_side and not (want_side & got_side):
                res.add(
                    "DPT-14", "warn", where,
                    f"光位方向相反（分镜 {_side_label(want_side)}，"
                    f"提示词 {_side_label(got_side)}）："
                    "光从反面来，同一场的光影关系会翻转",
                )

        # 同一句写两遍：多半是兜底逻辑重复追加（如最终画面句被补了两次）。
        dup = find_repeated_clause(own_text)
        if dup:
            res.add(
                "DPT-15", "warn", where,
                f"提示词里同一句出现两遍：「{dup[:24]}」——"
                "视频模型会当成两个连续动作或两次运镜",
            )

    return res


#: 能一眼看出「换了个光源」的词。只收物理上互斥的几类：
#: 窗光/日光 vs 人造灯 vs 屏幕光——同一场里从窗光切到台灯，人物脸光必然跳。
_LIGHT_SOURCES = (
    "窗", "斜阳", "夕阳", "阳光", "日光", "天光", "月光", "霓虹", "街灯", "路灯",
    "台灯", "夜灯", "灯泡", "吊灯", "荧光灯", "灯管", "钨丝", "白炽", "烛", "火光",
    "屏幕", "手机屏", "闪光灯",
)

_SIDE_ZH = {"left": "左", "right": "右", "both": "左右都有"}


def _side_label(sides: set[str]) -> str:
    """"{left}" → "左"；两侧都有 → "左右都有"。"""
    if len(sides) == 1:
        return _SIDE_ZH[next(iter(sides))]
    return _SIDE_ZH["both"]


def _light_clauses(text: str) -> list[str]:
    """切出真正在讲光的子句。

    只在「讲光的那一小段」里找方向词，否则「左手腕的旧钢表」（人物锚）会被
    当成「光从左边来」——实测这是最容易误报的一处。
    """
    out: list[str] = []
    for part in re.split(r"[，。；;、]", str(text or "")):
        if not re.search(r"光|灯|阳|烛|霓虹|月", part):
            continue
        if re.search(r"手|腕|指|脚|肩|耳|眼|眉", part):  # 身体部位不是光位
            continue
        out.append(part)
    return out


def _light_profile(text: str) -> tuple[set[str], set[str]]:
    """从一段描述里抽出（光源集合, 方位集合）。抽不到就空集，空集不参与判定。"""
    clauses = _light_clauses(text)
    blob = "，".join(clauses)
    sources = {s for s in _LIGHT_SOURCES if s in blob}
    sides: set[str] = set()
    if re.search(r"左", blob):
        sides.add("left")
    if re.search(r"右", blob):
        sides.add("right")
    return sources, sides


def _anchor_similarity(anchor: str, prompt: str) -> float:
    """锚在提示词里的保留程度：0-1。

    用最长公共子序列比值衡量「锚被改写了多少」——锚原样出现时为 1.0。
    """
    if not anchor or not prompt:
        return 0.0
    if anchor in prompt:
        return 1.0
    matcher = SequenceMatcher(None, anchor, prompt)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(anchor)


# ---------------------------------------------------------------------
# 三、跨链路断言
# ---------------------------------------------------------------------


def assert_pipeline(
    breakdowns: list[SceneBreakdown],
    shots: list[Shot],
    *,
    script: Script | None = None,
    style_guide: StyleGuide | None = None,
) -> AssertionResult:
    """检查导演 → 摄影指导这条链路的一致性。"""
    res = AssertionResult(target="pipeline")

    bd_ids = {s.id for bd in breakdowns for s in bd.shots}
    for s in shots:
        where = f"{s.scene_id}/{s.id}"
        if s.id not in bd_ids:
            res.add(
                "LNG-01", "critical", where,
                "摄影指导产出了导演没拆过的镜头（镜头集合对不上）",
            )
        if not (s.video_prompt or "").strip():
            res.add("LNG-02", "critical", where, "该镜头最终没有提示词，无法提交出片")

    # 剧本里的场景都应该被拆到
    if script is not None:
        covered = {bd.scene_id for bd in breakdowns}
        for sc in script.scenes:
            if sc.id not in covered:
                res.add("LNG-03", "critical", sc.id, "剧本里这场戏没有被导演拆解")

    # 分镜的光线与风格指南的光源类型必须对得上。
    #
    # 实测来源：`dialogue_tension` 的录制样本里，美术指导写「头顶一盏裸露的钨丝灯泡、
    # 桌面被照出一圈暖黄」，导演的分镜 `lighting` 却写「头顶一根冷白日光灯管」。
    # 提示词照抄了环境锚（所以锚复用检查全绿），但两层的**物理设定**是打架的：
    # 观众看到的是暖黄钨丝，而分镜据以安排人脸明暗的是冷白日光灯。
    # 这类矛盾下游看不出来，只能在跨链路这一层抓。
    if style_guide is not None:
        guide_src, _ = _light_profile(
            "；".join(
                [
                    str(getattr(style_guide, "lighting_style", "") or ""),
                    *[str(v) for v in (style_guide.environment_descriptions or {}).values()],
                ]
            )
        )
        if guide_src:
            for s in shots:
                if not s.lighting:
                    continue
                shot_src, _ = _light_profile(s.lighting)
                if shot_src and not (shot_src & guide_src):
                    res.add(
                        "LNG-04", "warn", f"{s.scene_id}/{s.id}",
                        f"分镜光线的光源和风格指南冲突（分镜「{'、'.join(sorted(shot_src))}」，"
                        f"风格指南「{'、'.join(sorted(guide_src))}」）："
                        "同一场的光从美术指导到导演被定了两次，提示词只会照抄其中一处",
                    )

    return res


# ---------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------


def summarize(results: list[AssertionResult]) -> dict[str, Any]:
    """把多个断言结果压成可比较的统计（用于跨版本对比）。"""
    total_fail = sum(len(r.failures) for r in results)
    total_crit = sum(len(r.critical) for r in results)
    return {
        "targets": len(results),
        "failures": total_fail,
        "critical": total_crit,
        "warn": total_fail - total_crit,
        "by_rule": _count_by_rule(results),
        "all_passed": all(r.passed for r in results),
    }


def _count_by_rule(results: list[AssertionResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        for f in r.failures:
            counts[f.rule] = counts.get(f.rule, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def format_failures(results: list[AssertionResult], *, limit: int = 40) -> str:
    """把失败列表渲染成可读报告。"""
    lines: list[str] = []
    shown = 0
    for r in results:
        if not r.failures:
            continue
        lines.append(f"── {r.target} ──")
        for f in r.failures:
            if shown >= limit:
                lines.append(f"  … 另有 {sum(len(x.failures) for x in results) - shown} 条未显示")
                return "\n".join(lines)
            lines.append(f"  {f}")
            shown += 1
    return "\n".join(lines) or "（无失败）"
