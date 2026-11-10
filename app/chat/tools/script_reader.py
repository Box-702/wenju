# =====================================================================
# script_reader.py —— 「读作品」域工具：read_script
#
# 补的能力缺口（docs/PLAN-mediago-reference.md C1，对标 mediago-drama）：
# 此前对话 Agent 只能读**小说原文**（read_source/search_source），
# 读不到剧本正文、分镜、视觉资产——用户说「把林墨那场戏改一下」，
# Agent 不知道那场戏现在长什么样，只能靠 get_status 的摘要猜。
#
# 为什么是一个工具四种视图，而不是 read_scene / read_shots / read_style：
# get_status 合并五查询的同一教训（ARCHITECTURE 3.1）——工具越多模型越容易
# 选错，选错后一本正经说瞎话。`part` 就是它的 `detail` 同款设计。
#
# 输出为什么是「可读台本」而不是 JSON：模型读散文比读裸字段稳，
# 且节拍 id（beat_xxx）原样保留——它正是 run_adaptation 定位改动的把手。
#
# 不抄 mediago 的「cwd 即上下文」：我们没有原生文件工具，等价物就是这个按需视图。
# =====================================================================

from __future__ import annotations

from langchain_core.tools import tool

from ...media.assets import (
    CHAR_APPEARANCES,
    ENV_DESCRIPTIONS,
    REFERENCE_IMAGES,
    VOICE_CASTING,
    build_index,
    lookup,
    normalize_style,
)
from .context import ToolContext

# 单次返回的软上限（字符）。超了截尾并明说截了——诚实的「略」比静默截断好
# （与覆盖诚实化、generate_script 的截断话术同一原则）。
_MAX_RETURN_CHARS = 6000

# 镜头提示词状态的显示词
_PROMPT_STATE = {"approved": "已批准", "needs_review": "待审", "draft": "草稿"}


def build(ctx: ToolContext) -> list:
    """装配「读作品」工具。"""
    store = ctx.store

    def _scene_script_text(sc, names: dict[str, str]) -> str:
        """一场戏 → 台本式可读文本（beat id 保留在行首，供 run_adaptation 引用）。"""
        head = [
            f"## {sc.id}《{sc.title}》",
            f"地点：{names.get(sc.location_id, sc.location_id)}"
            + (f"｜时间：{sc.time}" if sc.time else ""),
            f"出场：{'、'.join(names.get(c, c) for c in sc.characters)}",
            f"目的：{sc.purpose}",
            f"冲突：{sc.conflict}",
        ]
        if sc.entry_state or sc.exit_state:
            head.append(f"状态变化：{sc.entry_state or '—'} → {sc.exit_state or '—'}")
        body = ["", "正文（节拍按序）："]
        for b in sc.beats:
            if b.type == "dialogue":
                who = names.get(b.speaker or "", b.speaker or "?")
                emo = f"（{b.emotion}）" if b.emotion else ""
                body.append(f"- {b.id} 对白｜{who}{emo}：{b.line or b.text or ''}")
            elif b.type == "cue":
                body.append(f"- {b.id} 提示｜{b.text or ''}")
            else:
                body.append(f"- {b.id} 动作｜{b.text or ''}")
        # 兼容层：老版本没有 beats 时台本会空，退到 action/dialogue 两份兼容字段
        if not sc.beats:
            for a in sc.action:
                body.append(f"- （无 id）动作｜{a}")
            for d in sc.dialogue:
                who = names.get(d.speaker, d.speaker)
                body.append(f"- （无 id）对白｜{who}：{d.line}")
        if sc.adaptation_notes and sc.adaptation_notes.reason:
            body.append(f"\n改编说明：{sc.adaptation_notes.reason}")
        return "\n".join(head + body)

    def _shots_text(vv, jobs: dict) -> str:
        """镜头方案 → 逐镜可读清单（提示词批准状态 + 出片状态）。"""
        if vv is None or not vv.shots:
            return "（还没有镜头方案——需要先「拆镜头」。）"
        shots = [s for s in vv.shots if isinstance(s, dict) and s.get("id")]
        lines = [f"共 {len(shots)} 镜："]
        for s in shots:
            job = jobs.get(str(s.get("id")))
            video = "，未出片"
            if job is not None:
                video = {"succeeded": "，已出片", "failed": "，出片失败"}.get(job.status, "，出片中")
            state = _PROMPT_STATE.get(str(s.get("prompt_status") or ""), "无提示词")
            bits = [
                f"- {s.get('id')}（{s.get('shot_type') or '中景'}｜"
                f"{float(s.get('duration_sec') or 5):g}s｜提示词{state}{video}）"
            ]
            if s.get("subject"):
                bits.append(f" 主体：{s['subject']}")
            if s.get("action"):
                bits.append(f" 动作：{s['action']}")
            if s.get("dialogue_ref"):
                bits.append(f" 台词：{s['dialogue_ref']}")
            camera = s.get("camera")
            if isinstance(camera, dict) and camera.get("type"):
                path = f"—{camera['path']}" if camera.get("path") else ""
                bits.append(f" 运镜：{camera['type']}{path}")
            if s.get("final_frame"):
                bits.append(f" 终帧：{s['final_frame']}")
            lines.append("\n".join(bits))
        return "\n".join(lines)

    def _style_text(style: dict, idx) -> str:
        """视觉资产 → 全片风格 + 逐角色/场景的造型、定妆图、音色登记。"""
        if not style:
            return "（还没有风格指南——需要先「风格指南」；定妆图、音色同。）"
        ap = style.get(CHAR_APPEARANCES) or {}
        env = style.get(ENV_DESCRIPTIONS) or {}
        refs = style.get(REFERENCE_IMAGES) or {}
        voice = style.get(VOICE_CASTING) or {}
        parts = [
            f"画风：{style.get('visual_style') or '—'}",
            f"色调：{'、'.join(style.get('color_palette') or []) or '—'}",
            f"光线基调：{style.get('lighting_style') or '—'}",
            f"摄影风格：{style.get('camera_style') or '—'}",
            "\n角色视觉档案：",
        ]
        for cid, cname in idx.by_id.items():
            if not cid.startswith("char_"):
                continue
            bits = []
            appearance = lookup(ap, cid, idx)
            if appearance:
                bits.append(f"造型：{appearance}")
            if lookup(refs, cid, idx):
                bits.append("定妆图：有")
            if lookup(voice, cid, idx):
                bits.append("音色：已配")
            parts.append(f"- {cname}｜" + ("；".join(bits) if bits else "（未登记）"))
        parts.append("\n场景视觉档案：")
        for lid, lname in idx.by_id.items():
            if not lid.startswith("loc_"):
                continue
            bits = []
            desc = lookup(env, lid, idx)
            if desc:
                bits.append(f"环境：{desc}")
            if lookup(refs, lid, idx):
                bits.append("场景图：有")
            parts.append(f"- {lname}｜" + ("；".join(bits) if bits else "（未登记）"))
        return "\n".join(parts)

    @tool
    def read_script(project_id: str = "", part: str = "scenes", query: str = "") -> str:
        """读这部作品自己的创作文档——剧本正文、镜头方案、视觉资产（小说原文用 read_source，别混）。
        参数：project_id=项目 id（可省略，默认当前作品）；part 选读哪一面：
        scenes=一场戏的台本正文（含节拍 id，改编定位靠它）/ outline=全部场景一行一场目录 /
        shots=镜头清单与每镜的批准、出片状态 / style=风格指南与逐角色的造型定妆图音色登记；
        query 只对 scenes 生效：按场景标题、场景 id 或人物名定位到那一场。
        用户说「那场戏现在怎么写的 / 都哪些场 / 哪些镜头还没批 / 谁还没定妆照」就用这个；
        要先看目录就 part=outline，再带 query 读具体某场。"""
        pid = (project_id or "").strip() or (ctx.current_project_id or "")
        if not pid:
            return "不知道要读哪部作品：先在对话里打开一部，或把 project_id 传给我。"
        p = ctx.project(pid)
        version = store.latest_version(p)
        if version is None:
            return "这部作品还没有剧本版本——先生成初稿（generate_script）。"
        script = version.script
        idx = build_index(script)
        names = dict(idx.by_id)
        part = (part or "scenes").strip().lower()

        if part == "outline":
            lines = [f"《{script.title}》共 {len(script.scenes)} 场（剧本版本 {version.id}）："]
            for sc in script.scenes:
                cast = "、".join(names.get(c, c) for c in sc.characters[:6])
                lines.append(f"- {sc.id}《{sc.title}》：{sc.purpose}（{cast}）")
            text = "\n".join(lines)
            return text

        if part == "shots":
            vv = ctx.latest_video_version(p)
            jobs = ctx.latest_job_by_shot(p.id) if vv is not None else {}
            text = _shots_text(vv, jobs)
        elif part == "style":
            style = normalize_style(store.get_video_style(p.id) or {}, script)
            text = _style_text(style, idx)
        elif part in ("scene", "scenes"):
            q = (query or "").strip()
            if q:
                matched = [
                    sc for sc in script.scenes
                    if q == sc.id or q in sc.title
                    or any(q in (names.get(c, c) or "") for c in sc.characters)
                ]
                if not matched:
                    titles = "、".join(f"《{sc.title}》" for sc in script.scenes[:10])
                    return f"没有匹配「{q}」的场景。现有场景：{titles}"
                if len(matched) > 1:
                    listing = "\n".join(f"- {sc.id}《{sc.title}》" for sc in matched)
                    return f"「{q}」命中 {len(matched)} 场，指定其中一场再读（把场景 id 作 query 传回来）：\n{listing}"
                text = _scene_script_text(matched[0], names)
            elif len(script.scenes) == 1:
                text = _scene_script_text(script.scenes[0], names)
            else:
                listing = "\n".join(f"- {sc.id}《{sc.title}》" for sc in script.scenes)
                text = (
                    f"这部作品有 {len(script.scenes)} 场，一次读一场。要哪场就带它的 id 当 query："
                    f"\n{listing}"
                )
        else:
            return f"不认识 part=「{part}」（可用：scenes / outline / shots / style）。"

        if len(text) > _MAX_RETURN_CHARS:
            text = text[:_MAX_RETURN_CHARS] + "\n……（太长截断了；想看后面的部分，缩小范围再读一次）"
        return text

    return [read_script]
