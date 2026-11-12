# =====================================================================
# novel2video.py —— 核心链路直跑脚本：输入小说，输出成片
#
# 目的：把「小说 → 剧本 → 拆镜 → 风格 → 定妆图 → 提示词 → 批准 →
# 批量出片 → 合成」这条链路从对话 Agent 里剥出来，纯后端一遍跑完。
# 用来体检流水线本身（结构、提示词、一致性），不经过 LLM 工具编排，
# 每一步的输入输出都直接可见、可复跑。
#
# 用法（在项目根目录）：
#   uv run python scripts/novel2video.py 素材/孔乙己.txt --title 孔乙己
#   可选：--out output/孔乙己_成片.mp4  --shot-limit 15
#        --no-dubbing  --no-subtitles
#
# 断点续跑：每一步的产物都实时落库（script_versions / video_versions /
# video_style / video_jobs / media_assets）。中途挂了不用从头再来：
#   uv run python scripts/novel2video.py --resume proj_xxxx
# 脚本会探测哪些步骤已有产物、直接跳过；已出片的镜头不会重复提交（省钱）。
#
# 说明：
#   - 依赖与产品链路完全同一套函数（app/pipeline、app/crew、app/media、
#     app/video），只是把对话工具层的「启动后台任务」换成同步直调；
#   - 批准环节：脚本代表用户把 Prompt 标为 approved（复刻批量审阅端点的
#     写入路径），这是无人值守的妥协，产品里仍是人工闸门（D-43）；
#   - 批次推进靠 app.main 的三个 on_complete 接线，脚本里手动调用；
#   - 日志双写：stdout + output/logs/<标题>_<时间>.log（UTF-8 带 BOM，
#     Windows 记事本/查看器直接可读，不再乱码）。
# =====================================================================

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_LOG_FH = None  # main() 里初始化；_t 双写用


def _t(msg: str) -> None:
    """带时间戳的进度行——整跑要几十分钟，日志要能对时。双写 stdout 与日志文件。"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except Exception:  # noqa: BLE001
            pass


def _open_log(title: str) -> Path:
    """UTF-8 带 BOM 的日志文件——Windows 自带查看器按 GBK 猜编码会满屏乱码，
    BOM 让它认准 UTF-8。"""
    log_dir = ROOT / "output" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{title}_{time.strftime('%m%d_%H%M%S')}.log"
    return path


def main() -> int:
    global _LOG_FH

    ap = argparse.ArgumentParser(description="小说 → 成片 一键直跑（纯后端，可断点续跑）")
    ap.add_argument("novel", nargs="?", default="", help="小说原文 txt 路径")
    ap.add_argument("--title", default="", help="作品名（默认取文件名）")
    ap.add_argument("--adaptation", default="短剧", help="改编类型（默认 短剧）")
    ap.add_argument("--out", default="", help="成片导出路径（默认 output/<title>_成片.mp4）")
    ap.add_argument("--shot-limit", type=int, default=0,
                    help="只出前 N 镜（分批试跑省预算；0=全部）")
    ap.add_argument("--resume", default="", metavar="PROJECT_ID",
                    help="续跑已有作品：跳过已完成的步骤，不重复烧钱")
    ap.add_argument("--no-dubbing", action="store_true", help="不配音")
    ap.add_argument("--no-subtitles", action="store_true", help="不烧字幕")
    args = ap.parse_args()

    if not args.resume and not args.novel:
        ap.error("需要小说路径，或 --resume <作品id> 续跑")

    title = args.title
    if not title:
        if args.resume:
            title = "续跑"
        else:
            title = Path(args.novel).stem
    _LOG_FH = _open_log(title).open("w", encoding="utf-8-sig")
    _t(f"日志：{_LOG_FH.name}  |  "
       f"{'续跑 ' + args.resume if args.resume else args.novel}")

    # ---- 0. 进程接线：与 app.main 启动时完全相同的三件事 ----
    from app.config import bootstrap_env, get_settings

    bootstrap_env()

    from app.deps import batch_manager, settings, store
    from app.llm.seed import seed_keys_from_env

    st = store()
    seed_keys_from_env(get_settings(), st)  # 幂等：.env key 进 DB（D-41）

    # 批次推进 / 任务落库 / 成片质检重 roll 全靠这三条 on_complete 接线。
    from app.main import _wire_video_batch, _wire_video_job_writeback, _wire_video_qa

    _wire_video_job_writeback()
    _wire_video_qa()
    _wire_video_batch()

    from app.deps import llm as get_llm

    llm = get_llm()
    if not llm.available:
        _t("!! 对话模型不可用（检查 DB 厂商 / .env key），终止")
        return 1

    project_id = ""
    state: dict = {}
    try:
        project_id = run_pipeline(llm, st, args, title, state)
        return 0
    except Exception:  # noqa: BLE001
        _t("!! 脚本异常终止：")
        for ln in traceback.format_exc().splitlines():
            _t("   " + ln)
        hint = state.get("project_id") or args.resume or ""
        if hint:
            _t(f"断点信息：作品 id={hint}")
            _t(f"续跑命令：uv run python scripts/novel2video.py --resume {hint}")
        else:
            _t("（还没建成作品，无需续跑；修复后直接重跑即可）")
        return 1
    finally:
        if _LOG_FH is not None:
            _LOG_FH.close()


def run_pipeline(llm, st, args, title: str, state: dict) -> str:
    """十步流水线。每步先探测 DB 里已有的产物，有就跳过（幂等续跑）。
    state["project_id"] 实时登记——异常时调用方能报出续跑命令。"""
    from app.domain import (
        SceneBreakdown,
        StyleGuide,
        normalize_adaptation_type,
    )

    # ---- 1. 项目 ----
    if args.resume:
        p = st.get_project(args.resume)
        if p is None:
            _t(f"!! 找不到作品 {args.resume}")
            sys.exit(1)
        _t(f"续跑作品：{p.id}《{p.title}》")
    else:
        novel_path = Path(args.novel)
        if not novel_path.is_absolute():
            novel_path = ROOT / novel_path
        raw_text = novel_path.read_text(encoding="utf-8")
        p = st.create_project(
            title=title or novel_path.stem,
            adaptation_type=normalize_adaptation_type(args.adaptation),
            language="zh-CN",
            raw_text=raw_text,
        )
        _t(f"项目已建：{p.id}《{p.title}》 原文 {len(raw_text)} 字")
    title = title or p.title
    state["project_id"] = p.id  # 断点登记：之后任何一步炸了都能报续跑命令

    # ---- 2. 剧本（已有则复用） ----
    version = st.latest_version(p)
    if version is not None:
        script = version.script
        _t(f"① 剧本已有，跳过：{len(script.scenes)} 场 · {len(script.characters)} 角色"
           f"（版本 {version.id}）")
    else:
        from app.pipeline.generation import generate_script as run_generation

        _t("① 剧本生成：故事圣经 → 场景/节拍 …")
        t0 = time.time()
        script, artifacts = run_generation(
            llm, settings(), title=p.title, raw_text=p.raw_text or "",
            adaptation_type=p.adaptation_type, language=p.language,
        )
        version = st.create_version(
            p, script, source_type="generation", label="脚本直跑初稿",
            notes=f"生成模式：{artifacts.get('mode', 'n/a')}", set_current=True,
        )
        _t(f"   完成（{time.time()-t0:.0f}s）：{len(script.scenes)} 场 · "
           f"{len(script.characters)} 角色 · 模式 {artifacts.get('mode')}")

    # ---- 3. 导演拆镜（breakdown 已落版本则复用） ----
    breakdown_data = st.get_version_breakdown(version.id)
    if breakdown_data:
        breakdowns = [SceneBreakdown.model_validate(b) for b in breakdown_data]
        n_shots = sum(len(b.shots) for b in breakdowns)
        _t(f"② 导演拆解已有，跳过：{n_shots} 镜")
    else:
        from app.crew.director import run_director

        _t("② 导演拆镜 …")
        style_now = st.get_video_style(p.id) or {}
        res = run_director(llm, script, adaptation_type=p.adaptation_type,
                           visual_style=style_now.get("visual_style"))
        if not res.success:
            _t(f"!! 导演失败：{res.summary}")
            sys.exit(1)
        breakdowns = res.data
        shots_plan = [s.model_dump() for bd in breakdowns for s in bd.shots]
        st.create_video_version(
            project_id=p.id, shots=shots_plan, source_type="agent",
            label="脚本直跑·导演拆解", notes=res.summary,
        )
        st.set_version_breakdown(version.id, [bd.model_dump() for bd in breakdowns])
        _t(f"   完成：{len(shots_plan)} 镜 · {res.summary[:80]}")

    # ---- 4. 美术指导：风格指南（已有色彩方案则复用） ----
    from app.crew.dp import language_for

    video_rows = [r for r in st.list_api_providers(kind="video") if r.enabled]
    lang = language_for(video_rows[0].name, script.language) if video_rows else "zh"
    style_data = st.get_video_style(p.id) or {}
    if style_data.get("color_palette") or style_data.get("character_appearances"):
        guide = StyleGuide.model_validate(style_data)
        _t("③ 风格指南已有，跳过")
    else:
        from app.crew.art_director import run_art_director

        _t("③ 美术指导：全片视觉风格 …")
        res = run_art_director(llm, script, language=lang,
                               visual_style=style_data.get("visual_style"))
        if not res.success:
            _t(f"!! 美术失败：{res.summary}")
            sys.exit(1)
        patch = res.data.model_dump()
        # 必须 merge 且剔掉空的 reference_images（浅合并会清空定妆图注册表）
        patch.pop("reference_images", None)
        st.merge_video_style(p.id, patch)
        style_data = st.get_video_style(p.id) or {}
        guide = StyleGuide.model_validate(style_data)
        _t(f"   完成：{res.summary[:80]}")

    # ---- 5. 定妆图（注册表非空则跳过——生图按张计费，不重复烧） ----
    registered = dict(style_data.get("reference_images") or {})
    if registered:
        _t(f"④ 定妆图已有 {len(registered)} 张，跳过")
    else:
        from app.media.refs import build_reference_assets

        _t("④ 定妆图与场景图（文生图 + 质检重试） …")
        t0 = time.time()
        refs = build_reference_assets(script, guide)
        current = st.get_video_style(p.id) or {}
        merged = {**(current.get("reference_images") or {}), **refs}
        st.merge_video_style(p.id, {"reference_images": merged})
        registered = merged
        _t(f"   完成（{time.time()-t0:.0f}s）：注册 {len(refs)} 张参考资产")

    # ---- 6. 摄影提示词 + 7. 批准 ----
    # 找最新的「带 video_prompt」版本：有则 DP 已跑过，直接进批准。
    versions = sorted(st.list_video_versions(p.id), key=lambda v: v.created_at)
    prompt_version = next(
        (v for v in reversed(versions)
         if v.shots and any(isinstance(s, dict) and s.get("video_prompt")
                            for s in v.shots)),
        None,
    )

    # 已出片的镜头不再重复批准/提交（断点续跑省钱的钥匙）。
    succeeded_shots = {
        j.shot_id for j in st.list_video_jobs(p.id)
        if j.status == "succeeded" and j.shot_id
    }

    if prompt_version is None:
        from app.crew.dp import run_dp
        from app.video import get_registry

        _t("⑤ 摄影指导：逐镜提示词 …")
        prov_label, max_dur = "", 0.0
        if video_rows:
            prov = get_registry().get_provider(
                video_rows[0].name, api_key=video_rows[0].api_key,
                base_url=video_rows[0].base_url)
            if prov:
                prov_label, max_dur = prov.label, prov.max_duration_sec
        res = run_dp(llm, script, breakdowns=breakdowns, style_guide=guide,
                     target_provider=video_rows[0].name if video_rows else "",
                     provider_label=prov_label, prompt_language=lang,
                     max_duration=max_dur)
        if not res.success:
            _t(f"!! 摄影失败：{res.summary}")
            sys.exit(1)
        shots_draft = [{**s.model_dump(), "prompt_status": "needs_review",
                        "prompt_review_note": ""} for s in res.data]
        prompt_version = st.create_video_version(
            project_id=p.id, shots=shots_draft, source_type="agent",
            label="脚本直跑·提示词待批准", notes=res.summary,
        )
        _t(f"   完成：{len(shots_draft)} 镜提示词已出草稿")
    else:
        _t(f"⑤ 提示词已有（版本 {prompt_version.id}），跳过摄影指导")

    # ⑥ 批准：续跑时**总是重新生成批准版本**——批次的提交名单来自这里，
    # 必须把已出片的镜头剔出去；若沿用旧批准版本，批次会把成功的镜头
    # 再提交一遍（claim 在终态已释放，拦不住），等于重复计费。
    from app.api.video import _style_context
    from app.crew.dramaturgy import refresh_prompt_audits

    shots = [dict(s) for s in prompt_version.shots if isinstance(s, dict)]
    limit = args.shot_limit if args.shot_limit > 0 else len(shots)
    n_appr = 0
    for s in shots:
        if (s.get("video_prompt") and n_appr < limit
                and s.get("id") not in succeeded_shots):
            s["prompt_status"] = "approved"
            n_appr += 1
    refresh_prompt_audits(shots, style_context=_style_context(prompt_version))
    approved_v = st.create_video_version(
        project_id=p.id, shots=shots, style_guide=prompt_version.style_guide,
        source_type="manual", label="脚本直跑·批量批准",
        notes="无人值守脚本代批（产品内仍是人工闸门 D-43）",
        parent_version_id=prompt_version.id,
    )
    n_ok = sum(1 for s in shots if s.get("prompt_status") == "approved")
    _t(f"⑥ 已批准 {n_ok}/{len(shots)} 镜"
       + (f"（跳过已出片 {len(succeeded_shots)} 镜）" if succeeded_shots else ""))

    # ---- 8. 批量出片 ----
    from app.video.service import BatchCreateRequest, generate_batch

    pending = sum(1 for s in approved_v.shots
                  if isinstance(s, dict) and s.get("prompt_status") == "approved")
    if pending:
        _t(f"⑦ 提交出片批次（{pending} 镜待出，串行推进） …")
        batch = generate_batch(p.id, BatchCreateRequest(
            mode="auto", version_id=approved_v.id))
        _t(f"   批次 {batch['id']} 启动：{batch['total']} 镜")

        t0 = time.time()
        last_done = 0
        while True:
            b = batch_manager().latest_for_project(p.id)
            if b is None or b.status in ("done", "failed", "aborted"):
                break
            done = len(b.results)
            if done != last_done:
                _t(f"   进度 {done}/{len(b.shot_ids)} 镜完成（{time.time()-t0:.0f}s）")
                last_done = done
            time.sleep(10)
        b = batch_manager().latest_for_project(p.id)
        ok_jobs = [j for j in st.list_video_jobs(p.id) if j.status == "succeeded"]
        _t(f"   批次结束：status={b.status if b else '?'} · 成功任务 {len(ok_jobs)} 个"
           f"（{time.time()-t0:.0f}s）")
        if b is None or b.status != "done":
            _t("!! 批次未全部完成——成片合成仍按已成功镜头继续（降级阶梯）")
    else:
        _t("⑦ 所有已批准镜头都已出片，跳过批次")

    # ---- 9. 成片合成 ----
    from app.video.finalize import finalize_film

    _t("⑧ 成片合成：实测时长校准 → 拼接 → 配音 → 字幕 …")
    info = finalize_film(
        p.id, version_id=approved_v.id,
        subtitles=not args.no_subtitles, dubbing=not args.no_dubbing,
        on_progress=lambda m: _t(f"   · {m}"),
    )
    _t(f"   合成完成：{info['shots_used']}/{info['shots_total']} 镜 · "
       f"{info['duration_sec']}s · 配音={info['dubbed']} 字幕={info['subtitled']}")
    if info.get("warnings"):
        # 逐镜缺失的警告太啰嗦：聚合成一条
        missing = [w for w in info["warnings"] if "还没有成片" in w]
        others = [w for w in info["warnings"] if "还没有成片" not in w]
        if missing:
            _t(f"   ⚠ {len(missing)} 镜未出片，已跳过")
        for w in others:
            _t(f"   ⚠ {w}")

    # ---- 10. 导出 ----
    asset = st.get_asset(info["video_asset_id"])
    if asset is None or not asset.content:
        _t("!! 成片资产缺失，无法导出")
        sys.exit(1)
    out = Path(args.out) if args.out else ROOT / "output" / f"{p.title}_成片.mp4"
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(asset.content)
    _t(f"✅ 成片已导出：{out}（{out.stat().st_size/1e6:.1f} MB）")
    _t(f"   作品 id={p.id}，可在界面里回看台本/分镜/提示词/版本链")
    return p.id


if __name__ == "__main__":
    sys.exit(main())
