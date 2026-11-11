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

# 任务状态 -> 中文展示。提示词状态用 context.PROMPT_STATUS_LABELS（单一来源）。
JOB_STATUS_LABELS = {
    "pending": "排队中", "queued": "排队中", "generating": "生成中",
    "succeeded": "已出片", "failed": "失败", "cancelled": "已取消",
}


def build(ctx: ToolContext) -> list:
    """装配出片域工具。"""
    store, runner = ctx.store, ctx.runner

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
    def submit_videos(project_id: str, shot_ids: str = "") -> str:
        """提交视频生成：为提示词已批准的镜头启动批量出片（串行推进，前一镜成片自动作为后一镜的接力参考）。参数：project_id=项目 id；shot_ids 可选（逗号分隔只出指定镜头），留空=全部已批准的镜头。"""
        p = ctx.project(project_id)
        v = ctx.latest_video_version(p)
        if v is None or not v.shots:
            return "还没有镜头方案：请先「拆解镜头」「撰写镜头提示词」并批准。"
        wanted = {s.strip() for s in shot_ids.split(",") if s.strip()}
        approved = [
            s["id"] for s in v.shots
            if isinstance(s, dict) and s.get("id") and s.get("video_prompt")
            and s.get("prompt_status") == "approved" and (not wanted or s["id"] in wanted)
        ]
        if not approved:
            return "没有可提交的镜头：提示词需要先经过人工批准。可以说「批准全部提示词」，或先看看镜头清单。"
        from ...video.service import VideoJobCreate, generate_batch, submit_video_job

        try:
            if wanted:
                # 指定镜头：逐镜直接提交（接力链主要服务整片连拍，这里不强求）。
                created = 0
                reused = 0
                for sid in approved:
                    shot = next(s for s in v.shots if isinstance(s, dict) and s.get("id") == sid)
                    _job, is_new = submit_video_job(p.id, VideoJobCreate(
                        shot_id=sid, provider="minimax", model="default",
                        prompt=shot.get("video_prompt") or "", version_id=v.id,
                    ))
                    created += 1 if is_new else 0
                    reused += 0 if is_new else 1
                # 幂等：已在跑的镜头不会重复提交（那会重复计费），
                # 所以要如实说「几个新建、几个本来就在跑」。
                if reused and not created:
                    return (f"这 {reused} 个镜头已经在生成了（没有重复提交，不会重复计费），"
                            "稍后可问我进度。")
                if reused:
                    return (f"已提交 {created} 个镜头的视频生成，另有 {reused} 个本来就在跑"
                            "（没有重复提交）。稍后可问我进度。")
                return f"已提交 {created} 个指定镜头的视频生成，稍后可问我进度。"
            # 整片：走批次（串行推进，前一镜成片自动作为后一镜接力参考）。
            from ...video.service import BatchCreateRequest

            batch = generate_batch(p.id, BatchCreateRequest(version_id=v.id))
        except Exception as e:  # noqa: BLE001  FastAPI HTTPException 也走这里，转成用户可读文本
            detail = getattr(e, "detail", None) or str(e)
            return f"提交视频生成失败：{detail}"
        if batch.get("status") == "failed":
            return f"批次启动失败：{batch.get('error') or '未知原因'}"
        ctx.collector["payloads"].append({
            "type": "batch_started", "batch_id": batch.get("id"), "project_id": p.id,
        })
        total = batch.get("total") or len(approved)
        return f"已开始出片：共 {total} 个镜头会按顺序生成，前一镜完成后自动作为后一镜的参考以保持连贯。可以随时问我「出片进度」。"

    @tool
    def rerun_video(project_id: str, shot_id: str) -> str:
        """重新生成某个镜头的视频：任务失败时重试；对成片不满意时重新出一版。参数：project_id=项目 id；shot_id=镜头 id（形如 shot_scene001_001）。"""
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
        from ...video.service import VideoJobCreate, retry_video_job, submit_video_job

        try:
            if job is not None and job.status in ("failed", "cancelled"):
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
                shot_id=shot_id, provider="minimax", model="default",
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
            _api_cancel_job(job.id)
        except Exception as e:  # noqa: BLE001
            detail = getattr(e, "detail", None) or str(e)
            return f"取消失败：{detail}"
        return (f"已取消镜头 {sid} 的生成任务（{job.id}）。镜头方案与已批准的提示词保持不变，"
                "随时可以重跑或继续其他镜头。")

    @tool
    def finalize_film(project_id: str, subtitles: bool = True, dubbing: bool = True) -> str:
        """把已生成成功的镜头合成为完整成片（拼装 + 台词配音 + 台词字幕，后台执行）。用户说「合成成片 / 拼成片 / 出完整视频」时调用。参数：project_id=项目 id，subtitles=是否烧台词字幕（默认开），dubbing=是否配台词语音（默认开，无台词或未配语音 key 时自动跳过）。"""
        from ...deps import finalize_manager as _manager

        p = ctx.project(project_id)
        _manager().start(p.id, subtitles=subtitles, dubbing=dubbing)
        opts = [o for o, on in (("配音", dubbing), ("字幕", subtitles)) if on]
        suffix = f"（含{'/'.join(opts)}）" if opts else ""
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
        get_status,
        approve_video_prompts,
        submit_videos,
        rerun_video,
        edit_shot_prompt,
        cancel_video_job,
        finalize_film,
    ]
