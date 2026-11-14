# =====================================================================
# dp.py —— 摄影指导 Agent（模型感知的视频提示词构建）
#
# 职责：把「剧本 + 导演镜头方案 + 美术风格指南」组装成「直接喂给视频生成模型」
# 的提示词。这是链路里专门针对视频模型做提示词工程的那一步。
#
# 架构要点（提示词优先 + 单一事实来源）：
#   - 视觉锚的唯一来源是美术指导的 StyleGuide：
#       character_appearances / environment_descriptions（存 id 键，用时转显示名）
#     本模块【逐字复用】它们拼进每个镜头，杜绝同一角色/场景跨镜漂移。
#   - LLM 只负责本步骤独有的部分：每个镜头出场哪些人 + 这一镜的动作与运镜。
#   - 模型感知：按目标 provider 的方言输出（中文 / 英文）并遵守其时长约束。
#   - 若没有风格指南，退化为由本步骤自己产出环境/人物锚（保证链路仍可跑）。
# =====================================================================

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..domain import SceneBreakdown, Script, Shot, StyleGuide
from ..llm import LLM
from ..media.assets import as_name_map, build_index
from ..skills import render_skill
from .base import CrewAgent, CrewTaskResult
from .dramaturgy import (
    audit_video_prompt,
    find_negations,
    mentions_camera,
    mentions_lighting,
    strip_camera_moves,
    strip_slop,
    strip_spoken_quotes,
)

log = logging.getLogger(__name__)


# 原生支持中文提示词的模型（其余按英文处理）。
# MiniMax 原生吃中文；Seedance（火山方舟豆包系）同为国内模型吃中文方言，
# 且官方音画台词句式 `角色说话：「…」` 是中文写法——判英文等于一边教
# 「台词写进提示词」一边把正文换成英文。
# （切镜语法按厂商分支：Seedance 2.0 用「镜头N（D秒）」报每机位秒数，
#   其余用时间戳——D-52 定案，A/B 实验见 ~/.wenju/runs/ab_dialect_1114。）
_ZH_PROVIDERS = {"minimax", "seedance"}

# 视觉风格兜底（未提供风格指南时使用）。
_DEFAULT_STYLE = "写实电影质感，色调统一，轻微胶片颗粒，画面稳定。"

# 每个镜头 prompt_body 的写作铁律。
#
# 这些规则解决的是同一类问题：视频模型只能照字面理解文本，凡是没写出来的，
# 它都会自己瞎猜——镜头会无理由漂移、光线会一直黏在脸上、人物之间没有距离感、
# 背景像复制粘贴。把「动作」之外的运镜、光影、空间、背景四类信息都写成文字，
# 生成结果才可控。（沿用导演方案里已经拆好的字段，不要在这里新造设定。）
# 导演方案里缺某类调度信息时的补位提示：空值不阻断生成，但要让 LLM 知道
# 「这里确实没写，你可以按剧情补足」，而不是默默漏掉这类信息。
# body 里是否提到了摄影机/镜头——用来判断 LLM 有没有把运镜写进去。
_CAMERA_WORDS = ("镜头", "摄影机", "摄像机", "运镜", "机位", "camera", "lens", "shot")


def _mentions_camera(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in _CAMERA_WORDS)


def _asset_tag(asset_id: str, language: str, name: str = "") -> str:
    """资产引用标记：提示词里用 **@名字(编号)** 指代一份资产。

    为什么这么写：
    - **@ 与用户在对话里引用资产的方式一致**（用户在输入框里写「@林然」），
      全站一套引用语法：对话里 @ 名字，提示词里 @ 名字(编号)。
    - **带编号**：用户拿到提示词后要能回答「这个人物/场景用的哪份资产」——
      顺着编号能定位到资产库条目，换一张定妆图就影响所有引用它的镜头；
      没有编号，锚只是提示词里的一段话，改资产 = 全量重写一遍提示词。
    - 名字给下游模型看，编号给人和系统看；缺编号时只留名字。
    """
    if not asset_id:
        # 没有编号 = 这段锚的归属不明（例如单环境兜底），宁可不标，
        # 也不写一个没有编号的 @名字（那会让下游以为它是一份真资产）。
        return ""
    label = (name or "").strip()
    return f"@{label}({asset_id})" if label else f"@({asset_id})"



_MISSING_HINTS: dict[str, dict[str, str]] = {
    "zh": {
        "camera_path": "（未给镜头轨迹：请按景别与速度补出合理的起点→终点与机位高度）",
        "spatial": "（未给空间关系：请按剧情补足人物间的相对距离、离镜头的远近与离地高度）",
        "background": "（未给背景人物：公共场所请补少量各自独立活动的背景人物，允许有人静止）",
    },
    "en": {
        "camera_path": "(no path given: infer a plausible start → end move and camera height)",
        "spatial": "(no spatial data: infer distances between characters, camera and ground)",
        "background": "(no extras given: add a few independently behaving background people)",
    },
}


def language_for(provider: str, script_language: str) -> str:
    """推断提示词语言：优先看目标模型方言，其次看剧本语言。"""
    p = (provider or "").strip().lower()
    if p in _ZH_PROVIDERS:
        return "zh"
    if p:
        return "en"
    return "zh" if str(script_language or "").lower().startswith("zh") else "en"


# 中/英两套提示词都外置成技能文件（app/skills/video-prompt-*/SKILL.md）。
# 正文里的十三条写作规则改动频率不低，放 .md 里改完即生效，不必动代码。
_SKILL_BY_LANGUAGE = {"zh": "video-prompt-zh", "en": "video-prompt-en"}


def _fmt_ts(sec: float) -> str:
    """00:02.800 风格的时间戳（MiniMax 官方增强提示词的母语格式）。"""
    m, s = divmod(max(0.0, sec), 60)
    return f"{int(m):02d}:{s:06.3f}"


_LIGHT_RE = re.compile(
    r"([^，。,]*(?:光|逆光|侧光|顶光|灯光|烛光|日光|阳光|月光|"
    r"light|lighting|backlit|rim)[^，。,]*)")

# 官方音画台词句式（台词兜底的「已写过」判据）：冒号挂引号才算——
# 招牌引用（写下「…」）没有冒号前缀，不算台词（2026-11-14 002_015 实测：
# 旧判据「任意引号」让粉板文字顶掉了掌柜认领的台词，画面变哑剧）。
_OFFICIAL_DLG_RE = re.compile(r"[:：]\s*(?:[「『“\"]|\{)")


def _light_phrase(env: str) -> str:
    """从环境段里摘光线短句（lock line 用）：取第一个含光词的从句，没有就空。"""
    if not env:
        return ""
    m = _LIGHT_RE.search(env)
    return m.group(1).strip("，。, ") if m else ""


def _fold_group_prompts(shots: list[Shot], parts: dict[str, dict[str, str]],
                        *, language: str, provider: str = "") -> None:
    """把同 group 的机位折叠成一条组提示词（就地写 group_prompt 到组头镜头）。

    切镜语法按厂商分支（D-52 定案，用户四维验收+切点数据同向）：
      MiniMax / Seedance 2.5： [Shot 1] 0-4s… / [Shot 2] At 00:04.000, cuts to…
      Seedance 2.0：          镜头1（4秒）：… 镜头2（6秒）：…
    为什么 2.0 用「镜头N（D秒）」：每机位**显式报时长**，模型把台词与表演
    装进窗口——语速/音调/叙事节奏被秒数锚住（用户看片：这四项全面优于
    时间戳版）；切点误差也小一个量级（-0.25/-0.29s vs -0.46/-1.12s）。
    时间戳写法在 2.0 无害（无文字渲染）但没人管台词长度，A/B 实验
    产物见 ~/.wenju/runs/ab_dialect_1114/。
    时间戳/秒数由组内 duration_sec 累加**代码算**——模型对精确时间支持不稳，
    所以绝不让 LLM 自己数秒。
    全局锚去重：同组成员的环境/人物/风格段是逐字复用的同一份（锚纪律），
    取组头那份即可；body 各机位保留。
    单镜组（group_id 空或只剩 1 员）不写 group_prompt——下游照旧用 video_prompt。
    """
    zh = language == "zh"
    prov = (provider or "").strip().lower()
    # 2.5 认时间戳（官方文档）；只有 2.0 走镜头N 方言。模型名拿不到时
    # 按 2.0 处理——DB 现行 seedance 配置就是 2.0 标准档。
    seedance_20 = prov == "seedance"
    by_gid: dict[str, list[Shot]] = {}
    for s in shots:
        if s.group_id:
            by_gid.setdefault(s.group_id, []).append(s)
    for gid, members in by_gid.items():
        members.sort(key=lambda x: x.group_index)
        if len(members) < 2:
            continue
        head_parts = parts.get(members[0].id, {})
        seg: list[str] = []
        # 全局锚段：环境 + 人物 + 风格（组头那份，成员逐字相同）
        for key in ("env", "cast"):
            v = head_parts.get(key, "")
            if v:
                seg.append(v)
        cursor = 0.0
        # lock line（seedance 借鉴清单 #3，2026-11-13）：模型跨切镜不保留任何
        # 提示词没重复的东西——组头的全局锚只管到第一个机位，后半组
        # 「光线变暖/换脸」就是这么漂的。每个机位块尾逐字重述一次
        # 身份+光线锚（代码拼，不让 LLM 自己记）；首块紧跟全局锚不重复。
        lock = "，".join(x for x in (head_parts.get("cast", ""),
                                     _light_phrase(head_parts.get("env", ""))) if x)
        for i, m in enumerate(members, start=1):
            p = parts.get(m.id, {})
            body = p.get("body", "") or (m.video_prompt or "")
            if seedance_20:
                # 豆包 2.0 方言：每机位显式报秒数——语速/节奏锚（D-52）
                lead = f"镜头{i}（{int(round(m.duration_sec))}秒）：{body}"
            elif i == 1:
                lead = (f"[Shot 1] 0-{int(round(m.duration_sec))}s，{body}"
                        if zh else f"[Shot 1] 0-{int(round(m.duration_sec))}s: {body}")
            else:
                lead = f"[Shot {i}] At {_fmt_ts(cursor)}, cuts to {body}"
            if i > 1 and lock:
                lead += f"（{lock}不变）" if zh else f" ({lock} unchanged)"
            seg.append(lead)
            cursor += max(0.0, m.duration_sec)
        style = head_parts.get("style", "")
        if style:
            seg.append(style)
        group_prompt = "。".join(x.rstrip("。") for x in seg if x) + "。"
        members[0].group_prompt = group_prompt
        # 审计对组提示词同样跑一遍（双重审计语义延伸到组头）
        members[0].prompt_audit = (members[0].prompt_audit or []) + [
            a for a in audit_video_prompt({"video_prompt": group_prompt},
                                          style_context=style)
            if a not in (members[0].prompt_audit or [])
        ]

# 否定句修复的系统提示词。
#
# 为什么需要这一轮：写作规则里已经写了「只写正向描述，禁止否定句」+ 交稿前逐句自查，
# 但概率模型仍会漏（实测 10 条 case 里约 1 条稳定漏掉）。规则管不住的部分，
# 用一次有界修复兜底——只改含否定词的句子，其余一个字不动。
_NEGATION_REPAIR_SYSTEM = """你会收到若干条「镜头提示词草稿」，其中个别句子含否定式表述
（「没有」「不」「避免」「禁止」「不是」「并非」）。

视频模型会把被否定的内容照画出来——「视线扫过空椅子但没有停留」会被画成停在椅子上，
「避免均匀平光」会被画成平光。

请把每一条里**含否定词的句子**改写成正向描述，只改这些句子，其余一个字都不要动：
- 「但没有停留」→「随即移开」
- 「眼睛没有看任何地方」→「目光涣散，焦点落在虚处」
- 「避免均匀平光」→「低调侧光，脸部一侧沉进阴影」
- 改不出来就把那句话整句删掉——不想要的细节，不提比否定更好。

只输出 JSON：{"shot_id": "改写后的完整 prompt_body"}
不要输出解释，不要加 markdown 围栏。"""


def _scene_list(raw: Any) -> list[dict[str, Any]]:
    """把模型返回的 raw 规整成 scenes 列表（兼容 dict / list 两种形状）。"""
    scenes = raw.get("scenes") if isinstance(raw, dict) else raw
    if isinstance(scenes, dict):
        return [scenes]
    return [s for s in scenes if isinstance(s, dict)] if isinstance(scenes, list) else []


def _bodies_by_shot(raw: Any) -> dict[str, str]:
    """抽出 {shot_id: prompt_body}。"""
    out: dict[str, str] = {}
    for scene in _scene_list(raw):
        for sh in scene.get("shots") or []:
            if isinstance(sh, dict) and sh.get("shot_id") is not None:
                out[str(sh["shot_id"])] = str(sh.get("prompt_body") or "")
    return out


def _set_body(raw: Any, shot_id: str, body: str) -> bool:
    """就地替换某个镜头的 prompt_body，返回是否命中。"""
    for scene in _scene_list(raw):
        for sh in scene.get("shots") or []:
            if isinstance(sh, dict) and str(sh.get("shot_id")) == shot_id:
                sh["prompt_body"] = body
                return True
    return False


def dp_system(*, language: str, provider_label: str, max_duration: float, has_anchors: bool) -> str:
    """按目标模型生成摄影指导的系统提示词（内容在 app/skills/video-prompt-{zh,en}）。"""
    if language == "zh":
        lang_rule = "用简体中文写（目标模型原生支持中文，中文描述更贴合画面）"
        json_note = "JSON 里的文本字段用中文"
    else:
        lang_rule = "Write the prompt in English"
        json_note = "JSON text fields in English"
    duration_note = f"单个镜头时长上限约 {max_duration:.0f} 秒" if max_duration else "单镜时长遵守镜头方案"

    return render_skill(
        _SKILL_BY_LANGUAGE.get(language, "video-prompt-zh"),
        provider_label=provider_label,
        duration_note=duration_note,
        lang_rule=lang_rule,
        json_note=json_note,
        has_anchors=has_anchors,
    )


def _norm_key(text: str) -> str:
    """去空白与标点的规范化串：用来判断最终画面是否已被 body 覆盖（模型常改写措辞）。"""
    return re.sub(r"[\s，,。；;、·「」『』\"'！!？?．.]+", "", str(text or ""))


def _covers(anchor: str, text: str) -> bool:
    """`text` 是否已经写过 `anchor` 这层意思（允许改写措辞，不允许只是碰巧同开头）。

    旧实现只比 `_norm_key(anchor)[:8]`：模型只要在锚前面加个主语
    （「他推开门的瞬间，雨水顺着伞骨……**雨停了**」），前 8 字就对不上，
    于是兜底逻辑把整句锚又追一遍——实测出现过同一句「最终画面」连着两遍，
    视频模型会把它当成两个连续动作。

    这里改成滑窗覆盖率：4 字窗命中 ≥60% 算写过（原样或接近原样）；
    4 字窗对小改动的容忍度不够（「冷光熄在他脸上」只命中 20%），
    所以再挂一条 2 字窗 ≥65% 的兜底——实测「已写过」与「真没写」在 2 字窗上
    分得很开（0.71 对 0.14），不会把该补的漏掉。
    """
    a, t = _norm_key(anchor), _norm_key(text)
    if not a:
        return True
    if a in t:
        return True
    if len(a) < 4:
        return False

    def _ratio(n: int) -> float:
        grams = {a[i : i + n] for i in range(len(a) - n + 1)}
        return sum(1 for g in grams if g in t) / len(grams)

    return _ratio(4) >= 0.6 or _ratio(2) >= 0.65


class DPAgent(CrewAgent):
    """摄影指导 Agent：构建模型感知的视频提示词。"""

    name = "dp"
    label = "摄影指导"
    emoji = "📹"

    def run(
        self,
        script: Script,
        *,
        breakdowns: list[SceneBreakdown] | None = None,
        style_guide: StyleGuide | None = None,
        target_provider: str = "",
        provider_label: str = "",
        prompt_language: str = "",
        max_duration: float = 0.0,
        **kwargs: Any,
    ) -> CrewTaskResult:
        """构建视频提示词。"""
        if not breakdowns:
            return CrewTaskResult(success=False, summary="没有收到导演的镜头方案")
        all_shots: list[Shot] = [s for bd in breakdowns for s in bd.shots]
        if not all_shots:
            return CrewTaskResult(success=False, summary="镜头方案中没有镜头")

        language = prompt_language or language_for(target_provider, script.language)
        label = provider_label or (target_provider or "通用模型")
        style = self._style_text(style_guide)
        has_anchors = bool(
            style_guide and (style_guide.character_appearances or style_guide.environment_descriptions)
        )

        if not self.llm.available:
            return self._fallback(all_shots, script, style, language, style_guide)

        user_prompt = self._build_prompt(script, breakdowns, style_guide, language, has_anchors)
        system = dp_system(
            language=language, provider_label=label,
            max_duration=max_duration, has_anchors=has_anchors,
        )
        try:
            raw = self._invoke_json(system, user_prompt)
            updated = self._apply(all_shots, raw, script, style, style_guide,
                                  language, provider=target_provider)

            # 否定句有界修复（只修 1 轮）：装配完仍有否定描述时，让模型只把那些句子
            # 改写成正向，再重新装配。规则层已经尽力，这里是确定性兜底。
            if self._repair_negations(updated, raw):
                updated = self._apply(all_shots, raw, script, style, style_guide,
                                      language, provider=target_provider)

            n = sum(1 for s in updated if s.video_prompt)
            flagged = sum(1 for s in updated if s.prompt_audit)
            audit_note = (
                f"；{flagged} 个镜头有优化提醒，审阅时可逐条查看"
                if flagged
                else "，全部通过质量检查"
            ) if n else ""
            lang_cn = "中文" if language == "zh" else "英文"
            return CrewTaskResult(
                data=updated,
                summary=f"已为 {n}/{len(updated)} 个镜头写好{lang_cn}视频提示词{audit_note}。",
                metadata={"audit_flagged": flagged},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("摄影指导 Agent 失败：%s", e)
            return CrewTaskResult(success=False, summary=f"摄影指导工作失败：{e}", errors=[str(e)])

    # ---- 组装 ----

    @staticmethod
    def _style_text(style_guide: StyleGuide | None, *, body: str = "") -> str:
        """把风格指南压成一句可拼进提示词的话——**只补本镜没写的部分**。

        风格指南是给「写这个镜头的人」看的设计依据，不是要逐字贴给视频模型的文本。
        整段贴进去会和本镜自己的描述打架，judge 实测的典型冲突：
        「镜头固定不动」的镜头，末尾又被贴上「保留细微的手持浮动」「偶尔略高俯看」；
        「冷绿日光灯为主光」的镜头，末尾又贴「暖黄灯为主光」。
        视频模型拿到两套矛盾的指令，只能随机挑一套执行。

        所以按「填空不覆盖」处理：
          - body 已经写了光影 → 不贴 `lighting_style`
          - body 已经写了摄影机/运镜 → 不贴 `camera_style`
          - 色调永远贴（配色不与单镜描述冲突，且是跨镜统一的必要信息）

        `camera_style` 本身还会再剥掉「列举具体运镜方式」的子句——它若写着
        「以缓慢横移、推近为主」，贴到哪里都会变成多运动指令。

        `body` 为空时（降级路径）全部贴上，此时没有本镜描述可冲突。
        """
        if not style_guide:
            return _DEFAULT_STYLE

        def _clean(text: str) -> str:
            return str(text).strip().rstrip("。，；;,.")

        bits: list[str] = []
        if style_guide.lighting_style and not mentions_lighting(body):
            bits.append(_clean(style_guide.lighting_style))
        if style_guide.camera_style and not mentions_camera(body):
            bits.append(_clean(strip_camera_moves(style_guide.camera_style)))
        if style_guide.color_palette:
            bits.append("主色调 " + "、".join(style_guide.color_palette[:4]))
        return "，".join(b for b in bits if b) or _DEFAULT_STYLE

    def _build_prompt(
        self,
        script: Script,
        breakdowns: list[SceneBreakdown],
        style_guide: StyleGuide | None,
        language: str,
        has_anchors: bool,
    ) -> str:
        """把剧本场景的戏剧上下文 + 镜头方案 + 已有视觉锚交给 LLM。"""
        char_map = {c.id: c.name for c in script.characters}
        loc_map = {loc.id: loc.name for loc in script.locations}
        scene_map = {sc.id: sc for sc in script.scenes}

        parts = [f"剧本：《{script.title}》", f"梗概：{script.logline}", ""]
        if has_anchors and style_guide:
            # 锚以 id 存、以名字展示：这里若不是转成名字，模型会看到一串 char_004。
            idx = build_index(script)
            env_names = as_name_map(style_guide.environment_descriptions, idx)
            char_names = as_name_map(style_guide.character_appearances, idx)
            parts.append("【已固定的视觉锚（不要改写，仅供你写动作时对齐）】")
            if env_names:
                for name, desc in list(env_names.items())[:8]:
                    parts.append(f"  环境·{name}：{desc}")
            if char_names:
                for name, desc in list(char_names.items())[:12]:
                    parts.append(f"  人物·{name}：{desc}")
            parts.append("")

        for bd in breakdowns:
            sc = scene_map.get(bd.scene_id)
            parts.append(f"===== 场景 {bd.scene_id} =====")
            if sc:
                parts.append(f"标题：{sc.title}")
                parts.append(f"地点：{loc_map.get(sc.location_id, sc.location_id)}　时间：{sc.time or '未定'}")
                parts.append(f"戏剧目标：{sc.purpose}")
                parts.append(f"冲突：{sc.conflict}")
                names = "、".join(char_map.get(cid, cid) for cid in sc.characters)
                parts.append(f"在场人物：{names}")
                a = bd.anchors
                anchor_bits = [
                    ("情绪", a.emotion), ("母题", a.motif), ("物体", a.object),
                    ("破裂", a.break_point), ("最终画面", a.final_image),
                ]
                if any(v.strip() for _, v in anchor_bits):
                    parts.append("本场五锚点：" + "；".join(f"{k} {v}" for k, v in anchor_bits if v.strip()))
                beat_lines: list[str] = []
                for b in sc.beats[:24]:
                    if b.type == "dialogue":
                        beat_lines.append(f"  [对白] {char_map.get(b.speaker or '', b.speaker or '?')}：{b.line}")
                    elif b.type == "action":
                        beat_lines.append(f"  [动作] {b.text}")
                if beat_lines:
                    parts.append("本场节拍：")
                    parts.extend(beat_lines)
            parts.append("")
            hints = _MISSING_HINTS.get(language, _MISSING_HINTS["zh"])
            parts.append("本场镜头方案：")
            for s in sorted(bd.shots, key=lambda x: x.order):
                parts.append(
                    f"  - {s.id} | {s.shot_type} | 运镜 {s.camera.type}"
                    + (f"/{s.camera.direction}" if s.camera.direction else "")
                    + f"/{s.camera.speed}"
                    + f" | 时长 {s.duration_sec:.0f}s"
                    + (f" | 功能 {s.function}" if s.function else "")
                )
                parts.append(f"      主体：{s.subject}")
                parts.append(f"      动作：{s.action}")
                if s.final_frame:
                    parts.append(f"      最终画面：{s.final_frame}")
                # 摄影机轨迹是视频模型能否「跟着走」的关键，缺了就显式点出来。
                if s.camera.type != "static":
                    parts.append(f"      运镜轨迹：{s.camera.path or hints['camera_path']}")
                # 机位高度对固定机位同样重要（仰角/贴地决定了画面心理感受）。
                if s.camera.height:
                    parts.append(f"      机位高度：{s.camera.height}")
                if s.lighting:
                    parts.append(f"      光线：{s.lighting}")
                parts.append(f"      空间关系：{s.spatial or hints['spatial']}")
                if s.background_action:
                    parts.append(f"      背景人物：{s.background_action}")
                elif s.spatial:
                    parts.append(f"      背景人物：{hints['background']}")
                if s.mood:
                    parts.append(f"      情绪：{s.mood}（只写身体线索，情绪名词本身不进提示词）")
            parts.append("")

        if has_anchors:
            parts.append("环境与人物锚已固定：请只输出每个镜头的 characters 与 prompt_body。")
        else:
            parts.append("请按 JSON 输出：每场一份 environment 与 characters，每个镜头一段 prompt_body。")
        parts.append(
            "写 prompt_body 时按上面十三条规则：动作与运镜并重（一种主要运动、不越轴、结尾不改景别）、"
            "保留呼吸感、光影写方向与相对关系、空间写人物编号与距离、背景人物各有各的事、"
            "只写看得见的东西、接触动作物理过程只给主事件（节拍≤两步、他人不再碰同一道具）、"
            "全篇正向描述无否定句、台词用官方音画句式写进提示词（`角色说话：「台词」`配姿态）、"
            "以 final_frame 的可见状态收尾、情绪翻译成身体线索并禁用占位词。"
        )
        return "\n".join(parts)

    def _repair_negations(self, updated: list[Shot], raw: Any) -> bool:
        """把仍有否定句的镜头的 prompt_body 改写成正向描述（就地改 raw）。

        返回 raw 是否被修改。**只做一轮**：修不掉的就交给逐镜审计去提醒，
        不要在模型调用上开无限循环——那会把一次出片变成三四次。
        """
        bodies = _bodies_by_shot(raw)
        payload = {
            s.id: bodies.get(s.id, "")
            for s in updated
            if bodies.get(s.id) and find_negations(f"{s.video_prompt or ''} {bodies.get(s.id, '')}")
        }
        if not payload:
            return False

        try:
            fixed = self._invoke_json(
                _NEGATION_REPAIR_SYSTEM,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except Exception as e:  # noqa: BLE001
            # 修复是增强而非必需：模型不配合时不能连累已经拿到的结果。
            log.warning("否定句修复失败（保留原稿并交给审计提醒）：%s", e)
            return False

        changed = False
        for shot_id, new_body in (fixed or {}).items():
            text = str(new_body or "").strip()
            if not text or text == payload.get(str(shot_id)):
                continue
            # 只接受「确实变好」的改写：仍然含否定词的不要，免得来回改更糟。
            if find_negations(text):
                continue
            if _set_body(raw, str(shot_id), text):
                changed = True
        if changed:
            log.info("否定句修复：改写了 %d 个镜头的提示词草稿", len(payload))
        return changed

    def _apply(
        self,
        shots: list[Shot],
        raw: Any,
        script: Script,
        style: str,
        style_guide: StyleGuide | None,
        language: str = "zh",
        provider: str = "",
    ) -> list[Shot]:
        """组装最终 video_prompt。

        环境/人物锚优先取美术风格指南（单一事实来源），逐字复用；缺失时退回
        本步骤 LLM 产出的锚。
        """
        scenes_raw = raw.get("scenes") if isinstance(raw, dict) else raw
        if isinstance(scenes_raw, dict):
            scenes_raw = [scenes_raw]
        if not isinstance(scenes_raw, list):
            raise ValueError(f"期望 scenes 数组，得到 {type(scenes_raw)}")

        env_by_scene: dict[str, str] = {}
        cast_by_scene: dict[str, dict[str, str]] = {}
        shot_meta: dict[str, dict[str, Any]] = {}
        for item in scenes_raw:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("scene_id") or "")
            env_by_scene[sid] = str(item.get("environment") or "")
            chars = item.get("characters") or {}
            if isinstance(chars, dict):
                cast_by_scene[sid] = {str(k): str(v) for k, v in chars.items() if v}
            for sh in item.get("shots") or []:
                if isinstance(sh, dict) and sh.get("shot_id"):
                    shot_meta[str(sh["shot_id"])] = sh

        # 规范锚（风格指南 = 单一事实来源）。存的是 id 键，拼提示词要用显示名——
        # 视频模型不认识 char_004，只认识「孔乙己」。
        _idx = build_index(script)
        canon_chars = as_name_map(style_guide.character_appearances if style_guide else {}, _idx)
        canon_envs = as_name_map(style_guide.environment_descriptions if style_guide else {}, _idx)
        loc_map = {loc.id: loc.name for loc in script.locations}
        scene_loc = {sc.id: loc_map.get(sc.location_id, "") for sc in script.scenes}
        char_map = {c.id: c.name for c in script.characters}
        # 名字 -> 资产编号（提示词里的映射标记用；重名时第一个命中，重名本身
        # 由 assets.resolve 拒绝猜测，不在这里补救）。
        char_ids = {c.name: c.id for c in script.characters}
        scene_map = {sc.id: sc for sc in script.scenes}

        updated: list[Shot] = []
        parts_by_shot: dict[str, dict[str, str]] = {}
        for shot in shots:
            meta = shot_meta.get(shot.id) or {}
            body = str(meta.get("prompt_body") or meta.get("video_prompt") or "")
            # 台词兜底：旧写法（引号直挂说话动词、无冒号）会渲染成含混假语音，
            # 机械剥掉；官方音画句式（`角色说话：「…」`）保留——native 音画同生
            # 模式下它就是成片台词人声的唯一来源（2026-11-13 判据放宽，D-50）。
            body = strip_spoken_quotes(body)
            # 占位词兜底：整段都是占位词的短句机械剥掉；混写句保留，交给逐镜审计提醒。
            body = strip_slop(body)

            # 环境锚：风格指南（按地点名）优先，其次本步骤 LLM 产出；
            # 短片常全片同一场景，若只有一个环境锚就直接用它兜底。
            env = canon_envs.get(scene_loc.get(shot.scene_id, ""), "") or env_by_scene.get(shot.scene_id, "")
            if not env and len(canon_envs) == 1:
                env = next(iter(canon_envs.values()))

            # 本镜出场人物名：优先取 LLM 的该镜 characters，否则退化为本场人物
            names = meta.get("characters")
            if not (isinstance(names, list) and names):
                sc = scene_map.get(shot.scene_id)
                names = [char_map.get(cid, cid) for cid in (sc.characters if sc else [])]
            names = [str(n) for n in names if str(n).strip()]

            # 补漏：body 里**点名出现**的本场人物，即使 LLM 的 characters 列表漏了，
            # 也要把锚补进去。否则这个角色在画面里露脸却没有外观锚，跨镜必然长相漂移
            # ——「视觉锚只产一次、下游逐字复用」这条原则不能靠模型自觉。
            sc = scene_map.get(shot.scene_id)
            if sc is not None:
                present = set(names)
                for cid in sc.characters:
                    cname = char_map.get(cid, cid)
                    if cname and cname not in present and cname in body:
                        names.append(cname)
                        present.add(cname)

            # 人物锚：只给**本镜主体**注详细外观。
            # 把全场每个人的完整外貌都塞进提示词，模型会把每个人画出来、还抢主体
            # ——2026-10-31《孔乙己》实测：一个「酒客甲开口」的特写里挤了四个人，
            # 最抢眼的是背景里戴帽喝酒的那位，主体反而成了右侧背影。
            # 主体判据：导演写的 subject → 本镜 body 里点名的人 → 兜底取第一个。
            llm_cast = cast_by_scene.get(shot.scene_id, {})
            focus: list[str] = []
            for src in (str(shot.subject or ""), body):
                hit = [n for n in names if n and n in src]
                if hit:
                    focus = hit
                    break
            if not focus:
                focus = names[:1]
            cast: list[str] = []
            for n in focus:
                desc = canon_chars.get(str(n)) or llm_cast.get(str(n))
                name_with_tag = _asset_tag(char_ids.get(str(n), ''), language, n) or n
                cast.append(f"{name_with_tag}：{desc}" if desc else name_with_tag)

            # LLM 偶尔整段漏掉运镜：导演方案里本来就有，这里兜底补上。
            # 判据是「body 里有没有提到摄影机」而不是逐字比对——模型通常会把
            # 轨迹用自己的话复述一遍，逐字比对会把同一段运动写两遍。
            if shot.camera.path and not _mentions_camera(body):
                label = "运镜轨迹：" if language == "zh" else "Camera path: "
                body = f"{body.strip().rstrip('。')}。{label}{shot.camera.path}".strip("。")

            # 台词兜底（2026-11-13 孔乙己重生成实测）：SKILL 规则 9 教了官方音画
            # 句式，deepseek 仍整片一字不写（21 镜 dialogue_ref 全认领、正文 0 句
            # 台词）——native 模式没有台词文字=成片没有台词人声。台词原文在剧本
            # 节拍里就有，代码按 dialogue_ref 拼 `角色说话：「…」`，不让它依赖模型自觉。
            # 判据（2026-11-14 收紧）：只认「冒号挂引号」的官方台词句式为已写——
            # 用「有任意引号」判会把招牌引用（粉板写下「孔乙己，十九文」）误当
            # 台词，把认领的对白顶掉，掌柜该说话的画面变哑剧（002_015 实测）。
            ref = str(shot.dialogue_ref or "").strip()
            if ref and not _OFFICIAL_DLG_RE.search(body):
                sc_obj = scene_map.get(shot.scene_id)
                for b in (sc_obj.beats if sc_obj else []):
                    if b.id == ref and (b.line or "").strip():
                        speaker = char_map.get(b.speaker or "", b.speaker or "")
                        verb = "角色说话" if language == "zh" else "says"
                        body = (f"{body.strip().rstrip('。')}。{speaker}{verb}："
                                f"「{b.line.strip()}」" if language == "zh" else
                                f"{body.strip().rstrip('.')}. {speaker} {verb}: "
                                f"\u201c{b.line.strip()}\u201d")
                        break

            # 最终画面兜底（U11）：模型把最后一帧当情绪目的地，body 必须以
            # final_frame 收尾；LLM 漏写时把导演方案里的结束帧补成最后一句。
            ff = str(shot.final_frame or "").strip()
            if ff and not _covers(ff, body):
                body = f"{body.strip().rstrip('。')}。{ff}".strip("。")

            # 风格段按本镜 body 重算（填空不覆盖）：body 已经交代了光影/运镜的，            # 就不再贴全片风格段的同类描述，避免两套指令打架。
            # 没有风格指南时退回调用方传入的 style 字符串。
            eff_style = (
                self._style_text(style_guide, body=body) if style_guide is not None else style
            )

            pieces: list[str] = []
            if env:
                # 环境锚的资产编号：只有当环境锚确实来自该场景地点（风格指南）
                # 时才标注；单环境兜底拿到的锚归属不明，宁可不标。
                sc_obj = scene_map.get(shot.scene_id)
                env_from_loc = (
                    canon_envs.get(loc_map.get(sc_obj.location_id, ""), "") if sc_obj else ""
                )
                env_tag = _asset_tag(
                    sc_obj.location_id if sc_obj and env_from_loc == env else "", language,
                    loc_map.get(sc_obj.location_id, "") if sc_obj else "",
                )
                # 环境锚写成「@场景名(编号)：描述」，编号让人能定位资产，
                # 名字让下游模型看得懂（跟对话里 @ 场景名 是同一套写法）。
                env_head = (env_tag + "：" + env) if env_tag else env
                pieces.append(env_head.strip().rstrip("。"))
            if cast:
                shift = "人物：" if language == "zh" else "Characters: "
                pieces.append(shift + "；".join(c.strip().rstrip("。") for c in cast))
            if body:
                pieces.append(body.strip().rstrip("。"))
            if eff_style:
                pieces.append(eff_style.strip().rstrip("。"))
            prompt = "。".join(p for p in pieces if p) + "。" if pieces else ""
            if prompt:
                shot.video_prompt = prompt
            # 第二道审计（出厂前双重审计）：结论随镜头落库，人工审阅时逐镜可见。
            # 传入风格段以排除「本镜没有的运镜」——风格段每个镜头都带一遍，
            # 不排除会把所有镜头都误判成「一镜多运镜」。
            shot.prompt_audit = audit_video_prompt(
                {"video_prompt": prompt}, style_context=eff_style
            )
            parts_by_shot[shot.id] = {
                "env": pieces[0] if env else "",
                "cast": next((p for p in pieces if p.startswith(("人物：", "Characters:"))), ""),
                "body": body.strip().rstrip("。"),
                "style": eff_style.strip().rstrip("。") if eff_style else "",
            }
            updated.append(shot)
        _fold_group_prompts(updated, parts_by_shot, language=language, provider=provider)
        return updated

    def _fallback(
        self,
        shots: list[Shot],
        script: Script,
        style: str,
        language: str,
        style_guide: StyleGuide | None,
    ) -> CrewTaskResult:
        """无模型时的回退：把风格指南的锚 + 镜头字段拼成基础提示词。"""
        loc_map = {loc.id: loc.name for loc in script.locations}
        scene_map = {sc.id: sc for sc in script.scenes}
        char_map = {c.id: c.name for c in script.characters}
        char_ids = {c.name: c.id for c in script.characters}
        _idx = build_index(script)
        canon_chars = as_name_map(style_guide.character_appearances if style_guide else {}, _idx)
        canon_envs = as_name_map(style_guide.environment_descriptions if style_guide else {}, _idx)

        for shot in shots:
            sc = scene_map.get(shot.scene_id)
            env = canon_envs.get(loc_map.get(sc.location_id, ""), "") if sc else ""
            names = [char_map.get(cid, cid) for cid in (sc.characters if sc else [])]
            cast = []
            for n in names:
                name_with_tag = _asset_tag(char_ids.get(str(n), ''), language, n) or n
                cast.append(f"{name_with_tag}：{canon_chars[n]}" if n in canon_chars else name_with_tag)
            zh = language == "zh"
            head = f"{shot.shot_type}，{shot.camera.type} 运镜" if zh else f"{shot.shot_type} shot, {shot.camera.type}"
            # 兜底路径没有风格指南的归属信息，标不了编号，只写描述本身。
            env_head = env
            pieces = [env_head, head, f"{shot.subject}。{shot.action}".strip("。")]
            if cast:
                pieces.append(("人物：" if zh else "Characters: ") + "、".join(cast))
            # 没有模型时也要把运镜/空间/背景这几类信息带上，否则提示词退化成一句话。
            if shot.camera.path:
                pieces.append(f"运镜轨迹：{shot.camera.path}" if zh else f"Camera path: {shot.camera.path}")
            if shot.camera.height:
                pieces.append(f"机位高度：{shot.camera.height}" if zh else f"Camera height: {shot.camera.height}")
            if shot.lighting:
                pieces.append(shot.lighting)
            if shot.spatial:
                pieces.append(f"空间关系：{shot.spatial}" if zh else f"Spatial: {shot.spatial}")
            if shot.background_action:
                pieces.append(
                    f"背景人物：{shot.background_action}" if zh else f"Background: {shot.background_action}"
                )
            if shot.mood:
                pieces.append(f"{shot.mood}氛围" if zh else f"{shot.mood} mood")
            pieces.append(style)
            shot.video_prompt = strip_spoken_quotes(
                strip_slop("。".join(p.strip().rstrip("。") for p in pieces if p) + "。")
            )
            shot.prompt_audit = audit_video_prompt(
                {"video_prompt": shot.video_prompt}, style_context=style
            )
        return CrewTaskResult(data=shots, summary=f"（无模型回退）为 {len(shots)} 个镜头拼装基础提示词")


def run_dp(
    llm: LLM,
    script: Script,
    *,
    breakdowns: list[SceneBreakdown] | None = None,
    style_guide: StyleGuide | None = None,
    target_provider: str = "",
    provider_label: str = "",
    prompt_language: str = "",
    max_duration: float = 0.0,
) -> CrewTaskResult:
    """快捷入口：构建模型感知的视频提示词。"""
    agent = DPAgent(llm)
    return agent.run(
        script,
        breakdowns=breakdowns,
        style_guide=style_guide,
        target_provider=target_provider,
        provider_label=provider_label,
        prompt_language=prompt_language,
        max_duration=max_duration,
    )
