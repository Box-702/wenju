# =====================================================================
# crew.py —— 剧组域工具：拆解 → 风格指南 → 定妆图 → 视频提示词
#
# 四个工具都是「启动后台任务」：实现跑在 app/crew/*（director / art_director /
# dp）与 app/media/refs，这里只负责参数校验、挂到 SubAgentRunner、回一句话。
# 没配 runner（测试 / 本地直跑）时退化为同步执行，保证链路可单测。
#
# 视觉锚只产一次、下游逐字复用：风格指南（含外貌 / 环境锚）merge 进
# video_style，是下游所有提示词的唯一来源——所以这里必须 merge 不能覆盖
# （人工选角 voice_casting 与锚存在同一份 JSON 里）。
# =====================================================================

from __future__ import annotations

from langchain_core.tools import tool

from .context import ToolContext


def _parse_notes(notes: str) -> dict[str, str]:
    """把多行「名字: 要求」解析成 {名字: 要求}。

    例：
        林然: 三十五岁男性，深灰风衣，眼神疲惫
        咸亨酒店: 暖黄油灯，木柜台
    """
    out: dict[str, str] = {}
    for line in str(notes or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sep = "：" if "：" in line else (":" if ":" in line else "")
        if not sep:
            continue
        name, _, text = line.partition(sep)
        name = name.strip()
        text = text.strip()
        if name and text:
            out[name] = text
    return out


def build(ctx: ToolContext) -> list:
    """装配剧组域工具。"""
    store, llm, runner = ctx.store, ctx.llm, ctx.runner

    @tool
    def breakdown_scenes(project_id: str, scene_ids: str = "") -> str:
        """启动导演 Agent，将剧本场景拆解为镜头方案。分析每场戏的戏剧目标和节奏需求，设计镜头类型、运镜、光线。在后台运行。参数：project_id=项目 id，scene_ids=场景 id 列表（逗号分隔，可选，不填则拆解全部场景）。"""
        p = ctx.project(project_id)
        version = store.latest_version(p)
        if version is None:
            return "还没有剧本版本，请先生成初稿。"
        script = version.script
        sids = [s.strip() for s in scene_ids.split(",") if s.strip()] or None

        def _run(task):
            from ...crew.director import run_director

            style = store.get_video_style(p.id) or {}
            result = run_director(
                llm, script, scene_ids=sids,
                adaptation_type=p.adaptation_type,
                visual_style=style.get("visual_style"),
            )
            if result.success:
                # 保存到 video_versions
                shots_data = []
                for bd in result.data:
                    for shot in bd.shots:
                        shots_data.append(shot.model_dump())
                store.create_video_version(
                    project_id=p.id,
                    shots=shots_data,
                    source_type="agent",
                    label="导演拆解",
                    notes=result.summary,
                )
                # 同时保存 breakdown 到 script_version
                breakdown_data = [bd.model_dump() for bd in result.data]
                store.set_version_breakdown(version.id, breakdown_data)
            return result.summary

        task_id = runner.start("导演场景拆解", _run, project_id=p.id) if runner else "local"
        ctx.collector["payloads"].append({
            "type": "sub_agent_started",
            "task_id": task_id,
            "name": "导演场景拆解",
            "project_id": p.id,
        })
        scope = f"{len(sids)} 个指定场景" if sids else "全部场景"
        return f"正在把{scope}拆解成镜头方案，完成后会出现在右侧「视频工作台」。"

    @tool
    def generate_style_guide(project_id: str, director_notes: str = "") -> str:
        """启动美术指导 Agent，生成视觉风格指南。包括色彩方案、光线风格、摄影风格、角色造型、环境描述。在后台运行。参数：project_id=项目 id，director_notes=导演额外创意意图（可选）。"""
        p = ctx.project(project_id)
        version = store.latest_version(p)
        if version is None:
            return "还没有剧本版本，请先生成初稿。"
        script = version.script

        def _run(task):
            from ...crew.art_director import run_art_director
            from ...crew.dp import language_for

            # 视觉锚的语言必须与下游提示词构建一致：从已启用的视频 provider 推断。
            lang = "zh"
            try:
                rows = [r for r in store.list_api_providers(kind="video") if r.enabled]
                if rows:
                    lang = language_for(rows[0].name, script.language)
            except Exception:  # noqa: BLE001
                pass
            style_now = store.get_video_style(p.id) or {}
            result = run_art_director(
                llm, script, director_notes=director_notes, language=lang,
                visual_style=style_now.get("visual_style"),
            )
            if result.success:
                # 风格指南（含角色造型 / 环境描述）随项目持久化，是下游视觉锚的唯一来源。
                # 必须 merge 而不是覆盖：资产库里人工指定的音色（voice_casting）与性别
                # 也存在同一份 JSON 里，整体覆盖会把用户改过的选角冲掉。
                patch = result.data.model_dump()
                # 美术指导**不生产**定妆图注册表，model_dump 永远带一个空
                # reference_images——浅合并会把它 {} 盖进现值，等于清空整个
                # 资产库定妆照（重跑一次风格指南就全丢，2026-11-11 审计发现）。
                patch.pop("reference_images", None)
                store.merge_video_style(p.id, patch)
            return result.summary

        task_id = runner.start("美术指导", _run, project_id=p.id) if runner else "local"
        ctx.collector["payloads"].append({
            "type": "sub_agent_started",
            "task_id": task_id,
            "name": "美术指导",
            "project_id": p.id,
        })
        return "正在统一全片的视觉风格（人物造型、环境光线），完成后会自动应用到每个镜头。"

    @tool
    def generate_video_prompts(project_id: str) -> str:
        """启动摄影指导 Agent，为镜头方案中的每个镜头生成视频生成 Prompt（语言跟随视频厂商，MiniMax 用中文）。需要先完成导演场景拆解。在后台运行。参数：project_id=项目 id。"""
        p = ctx.project(project_id)
        version = store.latest_version(p)
        if version is None:
            return "还没有剧本版本，请先生成初稿。"
        script = version.script

        # 读取已有的 breakdown 与风格指南（两者都随项目/版本持久化）
        breakdowns_data = store.get_version_breakdown(version.id)
        if not breakdowns_data:
            return "还没有完成导演场景拆解，请先运行「导演拆解」（breakdown_scenes）。"
        style_data = store.get_video_style(p.id)

        # 目标视频 provider：决定提示词方言（中文/英文）与单镜时长约束
        target_provider, provider_label, max_duration = "", "", 0.0
        try:
            from ...video import get_registry

            rows = [r for r in store.list_api_providers(kind="video") if r.enabled]
            if rows:
                target_provider = rows[0].name
                prov = get_registry().get_provider(
                    target_provider, api_key=rows[0].api_key, base_url=rows[0].base_url
                )
                if prov:
                    provider_label = prov.label
                    max_duration = prov.max_duration_sec
        except Exception:  # noqa: BLE001
            pass

        def _run(task):
            from ...crew.dp import run_dp
            from ...domain import SceneBreakdown, StyleGuide

            bds = [SceneBreakdown.model_validate(b) for b in breakdowns_data]
            sg = StyleGuide.model_validate(style_data) if style_data else None
            result = run_dp(
                llm, script, breakdowns=bds, style_guide=sg,
                target_provider=target_provider, provider_label=provider_label,
                max_duration=max_duration,
            )
            if result.success:
                # 摄影指导只提交草稿，人工批准前不得进入视频任务队列。
                shots_data = [
                    {
                        **s.model_dump(),
                        "prompt_status": "needs_review",
                        "prompt_review_note": "",
                    }
                    for s in result.data
                ]
                previous = p.current_video_version_id and store.get_video_version(
                    p.current_video_version_id
                )
                store.create_video_version(
                    project_id=p.id,
                    shots=shots_data,
                    source_type="agent",
                    label="视频提示词待审阅",
                    notes=f"{result.summary}；生成后需逐镜人工批准，才能提交视频任务。",
                    parent_version_id=previous.id if previous else None,
                )
            return result.summary

        task_id = runner.start("摄影指导", _run, project_id=p.id) if runner else "local"
        ctx.collector["payloads"].append({
            "type": "sub_agent_started",
            "task_id": task_id,
            "name": "摄影指导",
            "project_id": p.id,
        })
        return (
            "正在为每个镜头撰写视频提示词，完成后请在右侧「视频工作台」逐镜确认，"
            "确认后才会开始出片。"
        )

    @tool
    def generate_reference_assets(project_id: str, notes: str = "", entities: str = "") -> str:
        """启动参考资产流水线：为角色与场景生成定妆图，用视觉模型质检后注册为全局视觉参考，供视频生成锁住跨镜一致性。在后台运行。

        参数：project_id=项目 id；entities=只给这些角色/场景出图（逗号或顿号分隔的名字，
        例如「林然,咸亨酒店」），留空=全部；notes=用户写的画面要求，多行，每行「角色名或场景名: 要求」
        （例如「林然: 三十五岁男性，深灰风衣，眼神疲惫」）。notes 会拼进对应角色的 prompt，
        没写到的角色按风格指南里的锚生成。用户点名几个就只生成几个，其余的已有定妆图保持不变。
        """
        p = ctx.project(project_id)
        version = store.latest_version(p)
        if version is None:
            return "还没有剧本版本，请先生成初稿。"
        script = version.script
        only = [x.strip() for x in str(entities or "").replace("、", ",").split(",") if x.strip()]

        def _run(task):
            from ...domain import StyleGuide
            from ...media.refs import build_reference_assets

            style_raw = store.get_video_style(p.id)
            guide = StyleGuide.model_validate(style_raw) if style_raw else None

            def _progress(msg: str) -> None:
                if runner:
                    runner.update_step(task.id, msg, "running")

            refs = build_reference_assets(
                script, guide,
                on_progress=_progress,
                notes=_parse_notes(notes),
                entities=only or None,
            )
            if not refs and only:
                _progress("名单里的名字都对不上现有角色/场景，没有生成任何图")
                return "名单都对不上：没有生成任何参考图（没有误烧全量）。请核对角色/场景名字后重试。"
            # 写回风格指南：reference_images 按实体 **深合并**（新图覆盖同实体，
            # 未点名的旧图原样保留）——浅合并会整字典替换，用户点名 2 个角色
            # 实际重生成并覆盖整个资产库（2026-11-11 审计修掉的烧钱行为）。
            # 重读一次现值：生图在后台跑了几分钟，期间可能有人改过风格指南。
            current = store.get_video_style(p.id) or {}
            merged_refs = {**(current.get("reference_images") or {}), **refs}
            store.merge_video_style(p.id, {"reference_images": merged_refs})
            names = "、".join(refs)
            return f"已生成并注册 {len(refs)} 张参考资产：{names}"

        task_id = runner.start("参考资产", _run, project_id=p.id) if runner else "local"
        ctx.collector["payloads"].append({
            "type": "sub_agent_started",
            "task_id": task_id,
            "name": "参考资产",
            "project_id": p.id,
        })
        scope = f"（名单：{'、'.join(only)}）" if only else "（全部角色与场景）"
        return (
            f"正在生成定妆图{scope}，会自动做质量把关。"
            "完成后镜头会自动带上这些参考图，用于锁住跨镜头的角色与场景一致。"
        )

    return [
        breakdown_scenes,
        generate_style_guide,
        generate_reference_assets,
        generate_video_prompts,
    ]
