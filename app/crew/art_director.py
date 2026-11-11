# =====================================================================
# art_director.py —— 美术指导 Agent（视觉锚的唯一来源）
#
# 职责：产出全局视觉指南，其中的「角色造型」与「环境描述」是全片视觉一致性的
# **单一事实来源**（single source of truth）——下游提示词构建会逐字复用它，
# 不再各写各的，从根上避免同一角色/同一场景在不同镜头里长得不一样。
#
# 键约定：
#   - character_appearances / environment_descriptions：**落库时按角色/地点 id 做键**
#     （模型仍按名字写，parse 时用 media.assets.rekey 换成 id）。名字会被用户改，
#     id 不会——按名字存等于「改个名，定妆图和音色全丢」，而且不报错。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

from ..domain import Script, StyleGuide
from ..llm import LLM
from ..skills import render_skill
from .base import CrewAgent, CrewTaskResult

log = logging.getLogger(__name__)


def art_system(language: str = "zh") -> str:
    """美术指导的系统提示词（内容在 app/skills/style-guide/SKILL.md）。

    旧版在这里硬编码过一份 60 行的提示词副本——SKILL.md 外置后它成了
    return 之后不可达的死代码，且已落后于 SKILL.md（缺「光源类型沿用导演分镜」
    等新规则），2026-11-11 审计删除。唯一来源是技能文件。
    """
    lang_note = (
        "所有描述用简体中文写（目标视频模型原生支持中文）"
        if language == "zh"
        else "Write every description in English"
    )
    return render_skill("style-guide", lang_note=lang_note)


class ArtDirectorAgent(CrewAgent):
    """美术指导 Agent：产出风格指南（视觉锚的唯一来源）。"""

    name = "art_director"
    label = "美术指导"
    emoji = "🎨"

    # 实拍纹理词：动画/水墨画风的外貌锚里出现这些 = 画风串染（deepseek 实测会把
    # 「写发色瞳色」的指令听成「写皮肤毛孔」）。锚是定妆图与视频提示词的唯一来源，
    # 矛盾指令喂进模型，出来就是四不像——所以代码裁决，不靠提示词自律（D-05）。
    _REALISTIC_MARKERS = (
        "毛孔", "雀斑", "皮肤纹理", "油光", "日晒", "风吹", "粗糙", "皱纹",
        "胡茬", "痘印", "老年斑", "皮肤真实", "实拍", "照片质感",
    )

    @classmethod
    def _style_mismatch(cls, guide: "StyleGuide", visual_style: str | None) -> str | None:
        """外貌锚与画风是否串味。None=干净；字符串=给重跑的修正指令。

        只在非写实画风下生效（写实用这些词本来就该）；一个角色命中 2 个以上
        实拍纹理词才算——单个「粗糙的布料」是正当描述，不冤枉。
        """
        from ..pipeline.visual_styles import visual_style_key

        if visual_style_key(visual_style) == "realistic":
            return None
        bad: list[str] = []
        for name, text in (guide.character_appearances or {}).items():
            hits = [m for m in cls._REALISTIC_MARKERS if m in str(text or "")]
            if len(hits) >= 2:
                bad.append(f"{name}（{'、'.join(hits[:3])}）")
        if not bad:
            return None
        return (
            "上一版把这些角色的造型写成了实拍质感（" + "；".join(bad[:4]) + "）。"
            "本作品是动画画风：造型要写**发色、瞳色、发型轮廓、服装配色、线条与上色方式**，"
            "禁止皮肤毛孔、雀斑、油光、风吹日晒这类实拍纹理词。请重写这些角色的造型，其余保持不变。"
        )

    def run(
        self,
        script: Script,
        *,
        director_notes: str = "",
        language: str = "zh",
        visual_style: str | None = None,
        **kwargs: Any,
    ) -> CrewTaskResult:
        """生成视觉风格指南。

        Args:
            script: 完整剧本。
            director_notes: 导演的额外创意意图。
            language: 描述语言（zh / en），与目标视频模型对齐。
            visual_style: 项目画风（realistic/anime/3d/ink）。画风决定角色造型
                该怎么写——实拍写服装材质与年代感，二次元写发色、瞳色、描边与
                上色方式；同一段「外貌描述」在两种画风下不能通用。

        Returns:
            CrewTaskResult.data = StyleGuide
        """
        if not self.llm.available:
            return self._fallback(script, visual_style)

        user_prompt = self._build_prompt(script, director_notes, visual_style)
        try:
            raw = self._invoke_json(art_system(language), user_prompt)
            guide = self._parse_guide(raw, script, visual_style)
            # 画风锚护栏：串味锚带进下游（定妆图/提示词）会一路四不像。
            # 确定性检出 + 有界重跑一次；重跑仍不合格就保留（summary 里如实说）。
            mismatch = self._style_mismatch(guide, visual_style)
            retried = False
            if mismatch:
                log.warning("美术指导画风串染（%s），带修正指令重跑一次", mismatch[:80])
                retry_prompt = (
                    user_prompt
                    + "\n\n上一版存在以下问题，请修正后重新输出完整 JSON：\n"
                    + mismatch
                )
                raw2 = self._invoke_json(art_system(language), retry_prompt)
                guide2 = self._parse_guide(raw2, script, visual_style)
                retried = True
                if not self._style_mismatch(guide2, visual_style):
                    guide = guide2
            summary = (
                f"美术指导完成风格指南：{len(guide.color_palette)} 色、"
                f"{len(guide.character_appearances)} 角色造型、{len(guide.environment_descriptions)} 场景"
            )
            if retried and mismatch:
                summary += "（外貌锚初版串了实拍质感，已按动画画风重写）"
            return CrewTaskResult(data=guide, summary=summary)
        except Exception as e:  # noqa: BLE001
            log.warning("美术指导 Agent 失败：%s", e)
            return CrewTaskResult(success=False, summary=f"美术指导工作失败：{e}", errors=[str(e)])

    def _build_prompt(self, script: Script, director_notes: str, visual_style: str | None = None) -> str:
        from ..pipeline.visual_styles import get_visual_style

        spec = get_visual_style(visual_style)
        parts = [
            f"剧本：《{script.title}》",
            f"梗概：{script.logline}",
            f"类型：{script.adaptation.type if script.adaptation else '未定'}",
            f"主题：{'、'.join(script.themes)}",
            f"画风：{spec['label']}——{spec['render_note']}",
            "角色造型必须按这个画风来写（"
            + ("写实时写服装材质、年代感、面部细节；"
               if spec["voice_pool"] == "realistic"
               else "二次元/动画时写发色、瞳色、发型轮廓、服装配色与描边风格；")
            + "不要写成另一种画风的描述）。",
            "",
            "人物（请以这些名字为键写角色造型）：",
        ]
        for c in script.characters:
            parts.append(f"  - {c.name}（{c.role or '未定'}）：{c.personality or c.goal or '无描述'}")
        parts.append("\n地点（请以这些名字为键写环境描述）：")
        for loc in script.locations:
            parts.append(f"  - {loc.name}：{loc.description or '无描述'}")
        if director_notes:
            parts.append(f"\n导演意图：{director_notes}")
        return "\n".join(parts)

    @staticmethod
    def _parse_guide(
        raw: dict[str, Any], script: Script | None = None, visual_style: str | None = None,
    ) -> StyleGuide:
        """解析风格指南，并把两个「按名字索引」的表换成按 id。

        模型照旧按名字写（让它直接写 char_004 准确率很低），但落库前代码按剧本
        把名字映射成 id：名字是用户随时会改的，id 不会。这样「酒客甲」改名叫
        「短衣帮甲」时，外貌锚与定妆图仍然跟着这个人走。
        """
        from ..media.assets import build_index, rekey

        _idx = build_index(script)
        chars = raw.get("character_appearances") or {}
        envs = raw.get("environment_descriptions") or {}
        return StyleGuide(
            color_palette=raw.get("color_palette", []) or [],
            lighting_style=str(raw.get("lighting_style") or ""),
            camera_style=str(raw.get("camera_style") or ""),
            # 画风以项目设定为准；模型给的只作兜底（它常把 anime 写成 "动漫风"）。
            visual_style=visual_style or raw.get("visual_style") or "realistic",
            visual_references=raw.get("visual_references", []) or [],
            character_appearances=rekey({str(k): str(v) for k, v in chars.items() if v}, _idx)
            if isinstance(chars, dict) else {},
            environment_descriptions=rekey({str(k): str(v) for k, v in envs.items() if v}, _idx)
            if isinstance(envs, dict) else {},
        )

    @staticmethod
    def _fallback(script: Script, visual_style: str | None = None) -> CrewTaskResult:
        guide = StyleGuide(
            color_palette=["#1a1a2e", "#16213e", "#0f3460", "#e94560"],
            lighting_style="自然光为主",
            camera_style="稳定拍摄",
            visual_style=visual_style or "realistic",
        )
        return CrewTaskResult(data=guide, summary="（无模型回退）生成默认风格指南")


def run_art_director(
    llm: LLM,
    script: Script,
    *,
    director_notes: str = "",
    language: str = "zh",
    visual_style: str | None = None,
) -> CrewTaskResult:
    """快捷入口：执行美术指导风格指南生成。"""
    agent = ArtDirectorAgent(llm)
    return agent.run(script, director_notes=director_notes, language=language,
                     visual_style=visual_style)
