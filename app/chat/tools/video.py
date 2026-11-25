# =====================================================================
# video.py —— 出片域工具：批准 → 提交 → 重跑 → 素材 → 合成 → 查进度
#
# 这条链上「人的决定」是硬闸门：提示词必须先 approve 才能提交出片（submit
# 只挑 prompt_status == "approved" 的镜头）；合成成片在后台跑，进度一律
# 通过 get_status 查询——模型不许凭记忆报进度。
# =====================================================================

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from .context import PROMPT_STATUS_LABELS, ToolContext, short
from .generation_models import options as generation_options, resolve as resolve_generation_model

# 任务状态 -> 中文展示。提示词状态用 context.PROMPT_STATUS_LABELS（单一来源）。
JOB_STATUS_LABELS = {
    "pending": "排队中", "queued": "排队中", "generating": "生成中",
    "succeeded": "已出片", "failed": "失败", "cancelled": "已取消",
}


def build(ctx: ToolContext) -> list:
    """装配出片域工具。"""
    store, runner = ctx.store, ctx.runner

    @tool
    def list_generation_models(kind: str) -> str:
        """列出当前可用的图片或视频生成模型。kind=image 或 video。生成前用结果给用户发确认卡；不要猜模型名。"""
        try:
            rows = generation_options(store, kind)
        except ValueError as e:
            return str(e)
        if not rows:
            return "没有已配置且启用的生成模型，请先到模型设置配置。"
        project_id = ctx.current_project_id or ctx.collector.get("project_id")
        if kind == "image" and len(rows) > 1 and project_id:
            question = {
                "question": "这次生成使用哪个模型？", "multi": False,
                "options": [
                    {"id": f"q1o{i}", "label": f"{r['label']} · {r['model']}",
                     "desc": "按所选模型本次计费；价格请以厂商账单为准", "recommended": False,
                     "provider_id": r["provider_id"], "model_name": r["model"]}
                    for i, r in enumerate(rows, start=1)
                ],
            }
            choice = store.create_pending_choice(
                project_id=project_id,
                conversation_id=ctx.collector.get("conversation_id") or "",
                title="选择图片模型" if kind == "image" else "选择视频模型",
                questions=[question],
            )
            ctx.collector["payloads"].append({
                "type": "option_confirm", "choice_id": choice.id,
                "project_id": project_id, "title": choice.title,
                "questions": [question], "status": "pending",
            })
            return (
                "已向用户展示模型选择卡，本轮到此为止；等待用户点选确认后再生成。"
                "确认后直接调用生成工具；服务端会把这次选择应用到一次生成任务。"
                "不要向用户展示 provider_id，也不要猜测质量、速度或费用。"
            )
        return "\n".join(f"- {r['label']} / {r['model']}（provider_id: {r['provider_id']}）" for r in rows)

    @tool
    def approve_video_prompts(project_id: str, shot_ids: str = "") -> str:
        """发起「批准镜头提示词」的请求——批准正式生效需要用户确认（点对话里的确认卡片，或回复「批准」）。注意：出片花钱，最终决定权在服务端硬闸门手里，你只能发起，不能代替用户批准。参数：project_id=项目 id；shot_ids=镜头 id 列表（逗号分隔），留空=请求批准全部有待审提示词的镜头。"""
        p = ctx.project(project_id)
        current = ctx.latest_video_version(p)
        if current is None or not current.shots:
            return "还没有镜头方案——需要先「拆解镜头」再「撰写镜头提示词」。"

        wanted = {s.strip() for s in shot_ids.split(",") if s.strip()}
        targets = [
            s for s in current.shots
            if isinstance(s, dict) and s.get("id") and s.get("video_prompt")
            and (not wanted or s["id"] in wanted)
            and s.get("prompt_status") != "approved"
        ]
        if not targets:
            return "没有可批准的镜头（可能都已批准，或选中镜头没有提示词）。"
        ids = [s["id"] for s in targets]
        conv = ctx.collector.get("conversation_id") or ""
        request = store.create_pending_approval(
            project_id=p.id, conversation_id=conv,
            version_id=current.id, shot_ids=ids,
        )
        ctx.collector["payloads"].append({
            "type": "prompt_approval",
            "approval_id": request.id,
            "project_id": p.id,
            "version_id": current.id,
            "shot_ids": ids,
            "status": "pending",
        })
        return (
            f"已发起批准请求（{len(ids)} 个镜头），下面是对话里的确认卡片。"
            "**批准还没有生效**——要用户点「确认批准」或回复「批准」后才会写入批准状态，然后才能出片。"
        )

    @tool
    def submit_videos(
        project_id: str,
        shot_ids: str = "",
        reference_asset_ids: str = "",
        ratio: str = "",
        provider_id: str = "",
        model_name: str = "",
    ) -> str:
        """提交视频生成：为提示词已批准的镜头启动批量出片（串行推进，前一镜成片自动作为后一镜的接力参考）。参数：project_id=项目 id；shot_ids 可选（逗号分隔只出指定镜头），留空=全部已批准的镜头；reference_asset_ids 可选（逗号分隔的素材 id，当作本次出片的参考锚，如白模视频，id 从 list_assets 取）；ratio=画面比例（9:16 / 16:9 / 1:1 等，留空=用全局默认 16:9）。

        ratio 必须能传下来：用户刚回答过画幅（Agent 问过就得用），批量这条路
        不能把答案吞掉——2026-11-19 实测，Agent 问完画幅走批量提交，
        批量接口没有画幅参数，结果按默认横屏出片，问了等于没问。
        provider_id/model_name 是用户在确认卡上选中的本次模型；两者同时提供。"""
        p = ctx.project(project_id)
        v = ctx.latest_video_version(p)
        if v is None or not v.shots:
            return "还没有镜头方案：请先「拆解镜头」「撰写镜头提示词」并批准。"
        wanted = {s.strip() for s in shot_ids.split(",") if s.strip()}
        # 参考素材：只接受属于本作品的素材（别的作品的素材挂上去会串画面）
        refs: list[str] = []
        for aid in (reference_asset_ids or "").split(","):
            aid = aid.strip()
            if not aid:
                continue
            asset = ctx.store.get_asset(aid)
            if asset is None:
                return f"没有这个素材：{aid}。用 list_assets 看可用的素材 id。"
            if asset.project_id != p.id:
                return f"素材 {aid} 不属于本作品，不能当参考锚。请先导入到本作品。"
            refs.append(aid)
        from ...video.service import (
            BatchCreateRequest,
            VideoJobCreate,
            generate_batch,
            generation_units,
            pick_video_provider,
            submit_video_job,
            unit_approved,
        )

        # 视频厂商按 DB 里 enabled 的行来（别写死——写死过一次，用户选了方舟
        # 而 Agent 打去了欠费的 MiniMax 户，402；见 pick_video_provider 注释）。
        video_provider, video_model = pick_video_provider()
        if provider_id or model_name:
            if not provider_id or not model_name:
                return "请同时提供已确认的视频厂商 id 和模型名。"
            try:
                row, video_model = resolve_generation_model(store, "video", provider_id, model_name)
            except ValueError as e:
                return str(e)
            video_provider = row.name
        # 画幅：用户明确给过就照给（留空=全局默认）。两条提交路径都要带上——
        # 单镜提交早就支持 ratio，批量这条 2026-11-19 才补上。
        ratio_clean = (ratio or "").strip()
        extra_params: dict[str, Any] = {}
        if ratio_clean:
            extra_params["aspect_ratio"] = ratio_clean

        # 提交单位 = 生成单元（镜头组折叠后的一条视频；单镜自成一条）。
        units = generation_units(v.shots)
        ready = [u for u in units if u["prompt"] and unit_approved(v.shots, u)]
        not_approved = ("没有可提交的镜头：提示词需要先经过人工批准。"
                        "可以说「批准全部提示词」，或先看看镜头清单。")
        try:
            if wanted:
                # 指定镜头：按**生成单元**提交——镜头组是一条视频里的几个切镜，
                # 逐机位提交会把一段戏生成 N 遍（N 倍计费），而且组头提交的是
                # 折叠后的组提示词（unit_prompt），不是某一机位的单镜提示词。
                targets = [u for u in units if set(u["member_ids"]) & wanted]
                if not targets:
                    return (f"没有找到要出片的镜头（{'、'.join(sorted(wanted))}）。"
                            "可以先查镜头清单确认 id。")
                ready_ids = {u["shot_id"] for u in ready}
                submit_units = [u for u in targets if u["shot_id"] in ready_ids]
                if not submit_units:
                    return not_approved
                created = 0
                reused = 0
                for u in submit_units:
                    _job, is_new = submit_video_job(p.id, VideoJobCreate(
                        shot_id=u["shot_id"], provider=video_provider, model=video_model,
                        prompt=u["prompt"], version_id=v.id,
                        params=({**({"reference_asset_ids": refs} if refs else {}),
                                 **extra_params}),
                    ))
                    created += 1 if is_new else 0
                    reused += 0 if is_new else 1
                blocked = len(targets) - len(submit_units)
                parts: list[str] = []
                if created:
                    parts.append(f"已提交 {created} 个镜头的视频生成")
                if reused:
                    parts.append(f"另有 {reused} 个本来就在跑（没有重复提交，不会重复计费）")
                if blocked:
                    parts.append(f"{blocked} 个因提示词未批准或组内有机位待修改而跳过")
                return "；".join(parts) + "。稍后可问我进度。"
            if not ready:
                return not_approved
            # 整片：走批次（串行推进，前一镜成片自动作为后一镜接力参考）。
            batch = generate_batch(p.id, BatchCreateRequest(
                version_id=v.id, aspect_ratio=ratio_clean or None,
                provider=video_provider, model=video_model))
        except Exception as e:  # noqa: BLE001  FastAPI HTTPException 也走这里，转成用户可读文本
            detail = getattr(e, "detail", None) or str(e)
            return f"提交视频生成失败：{detail}"
        if batch.get("status") == "failed":
            return f"批次启动失败：{batch.get('error') or '未知错误'}"
        ctx.collector["payloads"].append({
            "type": "batch_started", "batch_id": batch.get("id"), "project_id": p.id,
        })
        total = batch.get("total") or 0
        return f"已开始出片：共 {total} 个镜头会按顺序生成，前一镜完成后自动作为后一镜的参考以保持连贯。可以随时问我「出片进度」。"

    @tool
    def generate_video(
        project_id: str,
        shot_id: str,
        style: str = "",
        ratio: str = "",
        duration: int = 0,
        provider_id: str = "",
        model_name: str = "",
    ) -> str:
        """生成一个镜头的视频（单镜出片）。参数：project_id=项目 id；shot_id=镜头 id；
        style=视觉风格（实拍 / 二次元 / 三维 / 水墨，留空=用项目已设定的）；
        ratio=画面比例（9:16 / 16:9 / 1:1）；duration=时长秒（0=用镜头设计的时长）。

        缺 style / ratio 时**先问用户**，不要自己替用户定——它们直接决定成片长什么样。
        提示词必须已批准；组镜头的出片单位是组头。
        provider_id/model_name 为本次模型选择，两者同时提供。
        """
        p = ctx.project(project_id)
        sid = (shot_id or "").strip()
        v = ctx.latest_video_version(p)
        if v is None or not v.shots:
            return "还没有镜头方案：请先「拆解镜头」「撰写镜头提示词」。"
        shot = next(
            (s for s in v.shots if isinstance(s, dict) and s.get("id") == sid), None,
        )
        if shot is None:
            return f"没有找到镜头 {sid}，先查镜头清单确认 id。"

        style_set = str((ctx.store.get_video_style(p.id) or {}).get("visual_style") or "")
        missing: list[str] = []
        if not (style or "").strip() and not style_set:
            missing.append("视觉风格（实拍 / 二次元 / 三维 / 水墨）")
        if not (ratio or "").strip():
            missing.append("画面比例（9:16 竖屏 / 16:9 横屏 / 1:1）")
        if missing:
            return ("出片前需要确认这几样（直接决定成片长什么样，不替你定）："
                    + "、".join(missing)
                    + "。用户回答后带参数再调一次。")
        from ...video.service import (
            VideoJobCreate,
            generation_units,
            pick_video_provider,
            submit_video_job,
            unit_prompt,
        )

        video_provider, video_model = pick_video_provider()
        if provider_id or model_name:
            if not provider_id or not model_name:
                return "请同时提供已确认的视频厂商 id 和模型名。"
            try:
                row, video_model = resolve_generation_model(store, "video", provider_id, model_name)
            except ValueError as e:
                return str(e)
            video_provider = row.name

        # 组镜头：提交单位是组头，发的是折叠后的组提示词
        unit = next(
            (u for u in generation_units(v.shots) if sid in set(u["member_ids"])), None,
        )
        target_id = unit["shot_id"] if unit else sid
        # 批准看**组头**（组是一条视频，批准的是整段）
        head = next(
            (s for s in v.shots if isinstance(s, dict) and s.get("id") == target_id), shot,
        )
        if head.get("prompt_status") != "approved":
            return (f"镜头 {target_id} 的提示词还没批准，出片前要先人工批准"
                    "（可以说「批准全部提示词」）。")
        # 提示词取**组头**的：组提交的原文是折叠后的组提示词（unit_prompt 单一口径）
        prompt = unit_prompt(head)
        params: dict[str, Any] = {}
        if (ratio or "").strip():
            params["aspect_ratio"] = ratio.strip()
        if duration:
            params["duration_sec"] = int(duration)
        try:
            _job, is_new = submit_video_job(p.id, VideoJobCreate(
                shot_id=target_id, provider=video_provider, model=video_model,
                prompt=prompt, version_id=v.id, params=params,
            ))
        except Exception as e:  # noqa: BLE001
            detail = getattr(e, "detail", None) or str(e)
            return f"提交失败：{detail}"
        head = "已提交" if is_new else "这个镜头已经在生成了（没有重复提交，不会重复计费）"
        return f"{head}镜头 {target_id} 的视频生成。稍后可问我进度。"

    @tool
    def rerun_video(project_id: str, shot_id: str, provider_id: str = "",
                    model_name: str = "") -> str:
        """重新生成某个镜头的视频：任务失败时重试；对成片不满意时重新出一版。参数：project_id=项目 id；shot_id=镜头 id（形如 shot_scene001_001）；provider_id/model_name=用户选定的本次模型。"""
        p = ctx.project(project_id)
        shot_id = shot_id.strip()
        v = ctx.latest_video_version(p)
        shot = next(
            (s for s in (v.shots if v else []) if isinstance(s, dict) and s.get("id") == shot_id),
            None,
        )
        if shot is None:
            return f"没有找到镜头 {shot_id}，可以先查镜头清单确认 id。"
        job = ctx.latest_job_by_shot(p.id).get(shot_id)
        from ...video.service import (
            VideoJobCreate,
            pick_video_provider,
            retry_video_job,
            submit_video_job,
        )

        video_provider, video_model = pick_video_provider()
        if provider_id or model_name:
            if not provider_id or not model_name:
                return "请同时提供已确认的视频厂商 id 和模型名。"
            try:
                row, video_model = resolve_generation_model(store, "video", provider_id, model_name)
            except ValueError as e:
                return str(e)
            video_provider = row.name

        try:
            if job is not None and job.status in ("failed", "cancelled") and not (provider_id and model_name):
                retry_video_job(job.id)
                return f"已重新排队镜头 {shot_id}（之前失败/取消的任务重试）。"
            if not shot.get("video_prompt") or shot.get("prompt_status") != "approved":
                return "该镜头的提示词还没批准，重跑前需要先批准。"
            # 还在跑就别再投一次——那会重复计费。要重出片先取消或等它出完。
            active = ctx.store.find_active_video_job(p.id, shot_id)
            if active is not None:
                return (f"镜头 {shot_id} 还在生成中（任务 {active.id}，状态 {active.status}），"
                        "没有重复提交。等它出完再决定要不要重跑；想立刻换一版就先取消那个任务。")
            _job, _created = submit_video_job(p.id, VideoJobCreate(
                shot_id=shot_id, provider=video_provider, model=video_model,
                prompt=shot.get("video_prompt") or "", version_id=v.id,
            ))
            return f"已提交镜头 {shot_id} 的重新生成，稍后可问我进度。"
        except Exception as e:  # noqa: BLE001
            detail = getattr(e, "detail", None) or str(e)
            return f"重跑失败：{detail}"

    @tool
    def edit_shot_prompt(project_id: str, shot_id: str, prompt: str) -> str:
        """修改某个镜头的视频提示词（生成新的镜头版本）。编辑会使批准失效：该镜头回到待审状态，需用户重新批准后才能提交出片。参数：project_id=项目 id；shot_id=镜头 id（形如 shot_scene001_001）；prompt=新提示词全文（按用户的意思写，不要自行压缩成另一段）。"""
        p = ctx.project(project_id)
        v = ctx.latest_video_version(p)
        if v is None or not v.shots:
            return "还没有镜头方案：请先「拆解镜头」「撰写镜头提示词」。"
        new_prompt = (prompt or "").strip()
        if not new_prompt:
            return "新的提示词不能为空。"
        shots = [dict(s) for s in v.shots if isinstance(s, dict)]
        target = next((s for s in shots if s.get("id") == shot_id.strip()), None)
        if target is None:
            return f"没有找到镜头 {shot_id}，可以先用 get_status(detail=\"shots\") 确认 id。"
        was_approved = target.get("prompt_status") == "approved"
        target["video_prompt"] = new_prompt
        target["prompt_status"] = "needs_review"
        target["prompt_review_note"] = "对话中编辑，待审"
        from ...crew.dramaturgy import refresh_prompt_audits

        # style_guide 是 dict property：安全取 camera_style（审计排除用）。
        style_ctx = str((v.style_guide or {}).get("camera_style") or "")
        refresh_prompt_audits(shots, style_context=style_ctx)
        version = store.create_video_version(
            project_id=p.id, shots=shots, style_guide=v.style_guide,
            source_type="manual", label="对话中编辑镜头提示词",
            notes=None, parent_version_id=v.id,
        )
        tail = "（该镜头之前是已批准状态，编辑后需要重新走用户批准。）" if was_approved else ""
        return (f"已更新镜头 {target['id']} 的提示词（新镜头版本 {version.id}），"
                f"状态重置为待审，等用户批准后才可以出片。{tail}")

    @tool
    def find_prompt_divergence(project_id: str) -> str:
        """检查「剧本台词」与「镜头提示词里的台词」是否一致（用户说「台词和剧本对不上」「配音念的不是这句」「字幕和画面说的不一样」时先调这个）。确定性比对、零花费：逐提交单元列出漏写/被改写的台词。参数：project_id=项目 id。"""
        from ...pipeline.repair import find_divergences

        p = ctx.project(project_id)
        v = ctx.latest_video_version(p)
        sv = store.latest_version(p)
        if v is None or not v.shots:
            return "还没有镜头提示词，先拆解镜头并生成提示词。"
        if sv is None:
            return "没有剧本版本，无法比对台词（比对的前提是剧本权威）。"
        shots = [dict(s) for s in v.shots if isinstance(s, dict)]
        rep = find_divergences(sv.script, shots)
        if not rep.divergences:
            return f"台词一致性检查通过：{rep.checked} 个提交单元的提示词台词与剧本逐字一致。"
        lines = [f"发现 {len(rep.divergences)} 处台词分叉（查了 {rep.checked} 个提交单元）："]
        for d in rep.divergences[:12]:
            mark = "漏写" if d.kind == "missing" else "改写"
            lines.append(f"  · {d.shot_id}（{mark}）{d.detail}")
        if len(rep.divergences) > 12:
            lines.append(f"  … 另有 {len(rep.divergences) - 12} 处")
        lines.append("要我按剧本原文修复吗？（修复后这些镜头会回到待审，需要重新批准才能出片。）")
        return "\n".join(lines)

    @tool
    def repair_prompt_dialogue(project_id: str) -> str:
        """把提示词里漏写/被改写的台词修复成剧本原文（逐字），修复的提交单元打回待审。用户确认修复后调用；配合 find_prompt_divergence 使用。参数：project_id=项目 id。"""
        from ...crew.dramaturgy import refresh_prompt_audits
        from ...pipeline.repair import repair_dialogue

        p = ctx.project(project_id)
        v = ctx.latest_video_version(p)
        sv = store.latest_version(p)
        if v is None or not v.shots or sv is None:
            return "缺少镜头方案或剧本版本，无法修复。"
        shots = [dict(s) for s in v.shots if isinstance(s, dict)]
        rep = repair_dialogue(sv.script, shots)
        if not rep.repaired_shots:
            return "台词与剧本一致，不需要修复。"
        style_ctx = str((v.style_guide or {}).get("camera_style") or "")
        refresh_prompt_audits(shots, style_context=style_ctx)
        version = store.create_video_version(
            project_id=p.id, shots=shots, style_guide=v.style_guide,
            source_type="manual", label="台词一致性修复",
            notes="；".join(rep.notes), parent_version_id=v.id,
        )
        done = [d for d in rep.divergences]
        return (
            f"已修复 {len(rep.repaired_shots)} 个提交单元的台词（新镜头版本 {version.id}）："
            f"漏写 {sum(1 for d in done if d.kind == 'missing')} 处、改写 {sum(1 for d in done if d.kind == 'wrong')} 处。"
            "这些镜头已回到待审——需要重新批准才能出片；已出片且想换版本的，说「重出这几镜」。"
        )

    @tool
    def replace_in_prompts(project_id: str, old_text: str, new_text: str, context: str = "") -> str:
        """在所有镜头提示词里做精确文字替换（用户点名「把 X 改成 Y」但不是台词分叉时，如场景道具名、称谓）。规则：某镜头内唯一匹配才动；出现多次必须带 context（紧邻的上下文片段）定位，工具不猜。改过的镜头打回待审。参数：project_id=项目 id；old_text=被替换文字；new_text=替换成；context=上下文片段（多处匹配时必填）。"""
        from ...crew.dramaturgy import refresh_prompt_audits
        from ...pipeline.repair import replace_text

        p = ctx.project(project_id)
        v = ctx.latest_video_version(p)
        if v is None or not v.shots:
            return "还没有镜头提示词，先拆解镜头并生成提示词。"
        shots = [dict(s) for s in v.shots if isinstance(s, dict)]
        changed, rejected = replace_text(shots, old_text, new_text, context=context)
        if not changed and not rejected:
            return f"没有镜头的提示词包含「{old_text[:30]}」，没有改动。"
        tail = ""
        if rejected:
            tail = "\n没动的镜头：" + "；".join(rejected[:5]) + "（带上下文再来一次）"
        if not changed:
            return "因多处匹配需要精确定位，这次没有改动。" + tail
        style_ctx = str((v.style_guide or {}).get("camera_style") or "")
        refresh_prompt_audits(shots, style_context=style_ctx)
        version = store.create_video_version(
            project_id=p.id, shots=shots, style_guide=v.style_guide,
            source_type="manual", label="提示词文字替换",
            notes=f"「{old_text[:40]}」→「{new_text[:40]}」", parent_version_id=v.id,
        )
        return (
            f"已在 {len(changed)} 个镜头的提示词里完成替换（新镜头版本 {version.id}），"
            f"这些镜头回到待审：{'、'.join(changed[:8])}"
            + ("…" if len(changed) > 8 else "") + "。" + tail
        )

    @tool
    def cancel_video_job(project_id: str, shot_id: str) -> str:
        """取消某个镜头正在进行的视频生成任务（排队中/生成中）。用户说「取消/停掉这一镜」时调用；对成片不满意想立刻重出时，先取消再 rerun_video。参数：project_id=项目 id；shot_id=镜头 id。"""
        p = ctx.project(project_id)
        sid = shot_id.strip()
        job = store.find_active_video_job(p.id, sid)
        if job is None:
            latest = ctx.latest_job_by_shot(p.id).get(sid)
            if latest is None:
                return f"镜头 {sid} 没有提交过视频任务，无需取消。"
            return (f"镜头 {sid} 的任务已是终态（{JOB_STATUS_LABELS.get(latest.status, latest.status)}），"
                    "不用取消；想重出一版直接说重跑。")
        from ...video.service import cancel_video_job as _api_cancel_job

        try:
            result = _api_cancel_job(job.id)
        except Exception as e:  # noqa: BLE001
            detail = getattr(e, "detail", None) or str(e)
            return f"取消失败：{detail}"
        # 本地取消 ≠ 服务商那边真的停了——MiniMax 没有取消端点，
        # 那条任务可能还在生成、费用照计。这话必须说，不能让用户误以为省钱。
        note = result.get("note") or ""
        tail = f"（{note}）" if note else ""
        return (f"已取消镜头 {sid} 的生成任务（{job.id}）{tail}。"
                "镜头方案与已批准的提示词保持不变，随时可以重跑或继续其他镜头。")

    @tool
    def finalize_film(project_id: str, subtitles: bool = False,
                      audio_mode: str = "native") -> str:
        """把已生成成功的镜头合成为完整成片（拼装 + 可选字幕，后台执行）。用户说「合成成片 / 拼成片 / 出完整视频」时调用。参数：project_id=项目 id，subtitles=是否烧台词字幕（默认关；用户明确要字幕、或画面台词需要可读时才传 true），audio_mode=声音模式：native（默认，用视频模型原生音轨，台词随提示词音画同生、唇形同步）/ dub（用 TTS 重新配音）/ silent（静片，台词只走字幕）。用户没特别要求声音时用默认 native；模型支持原生音画同生时不要改成 dub。"""
        from ...deps import finalize_manager as _manager

        p = ctx.project(project_id)
        mode = (audio_mode or "native").strip().lower()
        if mode not in ("native", "dub", "silent"):
            mode = "native"
        _manager().start(p.id, subtitles=subtitles, audio_mode=mode)
        voice = {"native": "原生音画同生", "dub": "TTS 配音", "silent": "静片"}[mode]
        suffix = f"（声音：{voice}{'，含字幕' if subtitles else ''}）"
        return (
            f"已开始合成成片{suffix}，正在后台拼装各镜头。"
            "完成后成片会出现在作品工作台里，稍等片刻或直接问「合成好了吗」。"
        )

    def _shots_digest(shots: list[dict[str, Any]], jobs: dict[str, Any]) -> str:
        """逐镜清单。只在用户明确要看具体镜头时用——62 镜全列会淹掉上下文。"""
        lines = ["【逐镜】"]
        for s in shots[:40]:
            sid = s["id"]
            prompt = PROMPT_STATUS_LABELS.get(s.get("prompt_status") or "draft", "待审")
            job = jobs.get(sid)
            video = JOB_STATUS_LABELS.get(job.status, "") if job else "未提交"
            if job is not None and job.status == "failed" and getattr(job, "error_message", None):
                video += f"（{short(job.error_message, 40)}）"
            subject = s.get("subject") or ""
            lines.append(f"- {sid}{'（' + subject[:18] + '）' if subject else ''}：提示词{prompt}，视频{video or '未提交'}")
        if len(shots) > 40:
            lines.append(f"（其余 {len(shots) - 40} 镜略）")
        return "\n".join(lines)

    @tool
    def get_status(project_id: str, detail: str = "summary") -> str:
        """查看项目现在走到哪一步了：剧本、镜头、出片、合成、后台任务，一次给全。
        用户问「进度怎么样 / 到哪了 / 拆好了吗 / 出片了吗 / 合成好了没 / 哪些还没批准」都调这个，
        不要凭记忆回答，也不要靠猜。参数：project_id=项目 id；detail=summary（默认，概况）
        或 shots（附逐镜清单，用户要看具体镜头时才用）。"""
        p = ctx.project(project_id)
        blocks: list[str] = []

        # 剧本
        version = store.latest_version(p)
        if version is None:
            blocks.append("【剧本】还没有版本，需要先生成初稿。")
        else:
            sc = version.script
            blocks.append(
                f"【剧本】{version.id}《{sc.title}》{len(sc.scenes)} 场 / {len(sc.characters)} 人"
            )
            # 对白预算：短剧写太多台词是下游拆爆的直接原因，这里顺手报一句。
            chars = sum(len(b.line or "") for s in sc.scenes for b in s.beats if b.type == "dialogue")
            if chars:
                blocks.append(f"【对白】{chars} 字（约 {chars / 4:.0f} 秒念白）")

        # 镜头与出片
        vv = ctx.latest_video_version(p)
        shots = [s for s in (vv.shots if vv else []) if isinstance(s, dict) and s.get("id")]
        if not shots:
            blocks.append("【镜头】还没有镜头方案，需要先拆解。")
        else:
            approved = sum(1 for s in shots if s.get("prompt_status") == "approved")
            jobs = ctx.latest_job_by_shot(p.id)
            done = sum(1 for s in shots if (j := jobs.get(s["id"])) and j.status == "succeeded")
            failed = sum(1 for s in shots if (j := jobs.get(s["id"])) and j.status == "failed")
            line = f"【镜头】{len(shots)} 镜，提示词已批准 {approved}，已出片 {done}"
            if failed:
                line += f"，失败 {failed}（可重出）"
            blocks.append(line)

        # 合成
        pid = p.id
        try:
            from ...deps import finalize_manager as _manager

            rec = _manager().latest_for_project(pid)
        except Exception:  # noqa: BLE001  合成模块未启用时不该拖垮状态查询
            rec = None
        if rec is None:
            blocks.append("【合成】还没合成过。")
        elif rec["status"] == "running":
            blocks.append(f"【合成】进行中：{rec['messages'][-1] if rec['messages'] else '正在准备'}")
        elif rec["status"] == "failed":
            blocks.append(f"【合成】失败：{rec['error']}")
        else:
            r = rec["result"] or {}
            flags = [
                "已配音" if r.get("dubbed") else "未配音",
                "已烧字幕" if r.get("subtitled") else "无字幕",
            ]
            blocks.append(
                f"【合成】已完成（{r.get('duration_sec', 0):.1f} 秒，"
                f"{r.get('shots_used', 0)}/{r.get('shots_total', 0)} 镜，{'/'.join(flags)}）"
            )

        # 后台任务：只报本作品的（2026-11-11 审计：此前拿全局任务表前 3 条，
        # A 作品的对话会把 B 作品的出片任务名与结果报给用户）。
        if runner is not None:
            tasks = runner.list_tasks(project_id=pid)[:3]
            if tasks:
                items = []
                for t in tasks:
                    st = {"pending": "排队中", "running": "进行中", "done": "已完成", "failed": "失败"}.get(t.status, t.status)
                    one = f"{t.name}{st}"
                    if t.status == "done" and t.result:
                        one += f"（{short(t.result, 60)}）"
                    items.append(one)
                blocks.append("【后台】" + "；".join(items))

        if str(detail).lower() in ("shots", "full", "镜头"):
            blocks.append(_shots_digest(shots, ctx.latest_job_by_shot(p.id)))
        return "\n".join(blocks)

    return [
        list_generation_models,
        get_status,
        approve_video_prompts,
        submit_videos,
        generate_video,
        rerun_video,
        edit_shot_prompt,
        find_prompt_divergence,
        repair_prompt_dialogue,
        replace_in_prompts,
        cancel_video_job,
        finalize_film,
    ]
