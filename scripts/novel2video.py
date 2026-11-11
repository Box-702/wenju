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
#   可选：--out output/孔乙己_成片.mp4  --no-dubbing  --no-subtitles
#
# 说明：
#   - 依赖与产品链路完全同一套函数（app/pipeline、app/crew、app/media、
#     app/video），只是把对话工具层的「启动后台任务」换成同步直调；
#   - 批准环节：脚本代表用户把全部 Prompt 标为 approved（复刻批量审阅
#     端点的写入路径：改状态 → 刷审计 → 开新版本），这是无人值守的妥协，
#     产品里这一步仍是人工闸门（D-43）；
#   - 批次推进靠 app.main 的三个 on_complete 接线，脚本里手动调用。
# =====================================================================

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _t(msg: str) -> None:
    """带时间戳的进度行——整跑要几十分钟，日志要能对时。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="小说 → 成片 一键直跑（纯后端）")
    ap.add_argument("novel", help="小说原文 txt 路径")
    ap.add_argument("--title", default="", help="作品名（默认取文件名）")
    ap.add_argument("--adaptation", default="短剧", help="改编类型（默认 短剧）")
    ap.add_argument("--out", default="", help="成片导出路径（默认 output/<title>_成片.mp4）")
    ap.add_argument("--shot-limit", type=int, default=0,
                    help="只出前 N 镜（分批试跑省预算；0=全部）")
    ap.add_argument("--no-dubbing", action="store_true", help="不配音")
    ap.add_argument("--no-subtitles", action="store_true", help="不烧字幕")
    args = ap.parse_args()

    novel_path = Path(args.novel)
    if not novel_path.is_absolute():
        novel_path = ROOT / novel_path
    raw_text = novel_path.read_text(encoding="utf-8")
    title = args.title or novel_path.stem

    # ---- 0. 进程接线：与 app.main 启动时完全相同的三件事 ----
    from app.config import bootstrap_env, get_settings

    bootstrap_env()

    from app.deps import batch_manager, settings, store, video_manager
    from app.llm.seed import seed_keys_from_env

    st = store()
    seed_keys_from_env(get_settings(), st)  # 幂等：.env key 进 DB（D-41）

    # 批次推进 / 任务落库 / 成片质检重 roll 全靠这三条 on_complete 接线。
    # 不接：批次不会推进（事件驱动）、video_jobs 表不更新、质检闭环失效。
    from app.main import _wire_video_batch, _wire_video_job_writeback, _wire_video_qa

    _wire_video_job_writeback()
    _wire_video_qa()
    _wire_video_batch()

    from app.deps import llm as get_llm

    llm = get_llm()
    if not llm.available:
        _t("!! 对话模型不可用（检查 DB 厂商 / .env key），终止")
        return 1

    # ---- 1. 建项目 ----
    from app.domain import normalize_adaptation_type

    p = st.create_project(
        title=title,
        adaptation_type=normalize_adaptation_type(args.adaptation),
        language="zh-CN",
        raw_text=raw_text,
    )
    _t(f"项目已建：{p.id}《{p.title}》 原文 {len(raw_text)} 字")

    # ---- 2. 剧本生成（底层流水线，不走对话工具的章节选段） ----
    from app.pipeline.generation import generate_script as run_generation

    _t("① 剧本生成：故事圣经 → 场景/节拍 …")
    t0 = time.time()
    script, artifacts = run_generation(
        llm, settings(), title=title, raw_text=raw_text,
        adaptation_type=p.adaptation_type, language=p.language,
    )
    version = st.create_version(
        p, script, source_type="generation", label="脚本直跑初稿",
        notes=f"生成模式：{artifacts.get('mode', 'n/a')}", set_current=True,
    )
    _t(f"   完成（{time.time()-t0:.0f}s）：{len(script.scenes)} 场 · "
       f"{len(script.characters)} 角色 · 模式 {artifacts.get('mode')}")

    # ---- 3. 导演拆镜 ----
    from app.crew.director import run_director

    _t("② 导演拆镜 …")
    style_now = st.get_video_style(p.id) or {}
    res = run_director(llm, script, adaptation_type=p.adaptation_type,
                       visual_style=style_now.get("visual_style"))
    if not res.success:
        _t(f"!! 导演失败：{res.summary}")
        return 1
    breakdowns = res.data
    shots_plan = [s.model_dump() for bd in breakdowns for s in bd.shots]
    dv = st.create_video_version(
        project_id=p.id, shots=shots_plan, source_type="agent",
        label="脚本直跑·导演拆解", notes=res.summary,
    )
    st.set_version_breakdown(version.id, [bd.model_dump() for bd in breakdowns])
    _t(f"   完成：{len(shots_plan)} 镜（模型自决）· {res.summary[:60]}")

    # ---- 4. 美术指导：风格指南 ----
    from app.crew.art_director import run_art_director
    from app.crew.dp import language_for
    from app.domain import StyleGuide

    _t("③ 美术指导：全片视觉风格 …")
    video_rows = [r for r in st.list_api_providers(kind="video") if r.enabled]
    lang = language_for(video_rows[0].name, script.language) if video_rows else "zh"
    res = run_art_director(llm, script, language=lang,
                           visual_style=style_now.get("visual_style"))
    if not res.success:
        _t(f"!! 美术失败：{res.summary}")
        return 1
    patch = res.data.model_dump()
    # 必须 merge 且剔掉空的 reference_images（工具层同款护栏：浅合并会清空定妆图注册表）
    patch.pop("reference_images", None)
    st.merge_video_style(p.id, patch)
    style_data = st.get_video_style(p.id) or {}
    guide = StyleGuide.model_validate(style_data)
    _t(f"   完成：{res.summary[:60]}")

    # ---- 5. 定妆图 / 场景图（生成 + 视觉质检） ----
    from app.media.refs import build_reference_assets

    _t("④ 定妆图与场景图（文生图 + 质检重试） …")
    t0 = time.time()
    refs = build_reference_assets(script, guide)
    current = st.get_video_style(p.id) or {}
    merged = {**(current.get("reference_images") or {}), **refs}
    st.merge_video_style(p.id, {"reference_images": merged})
    _t(f"   完成（{time.time()-t0:.0f}s）：注册 {len(refs)} 张参考资产")

    # ---- 6. 摄影指导：视频提示词 ----
    from app.crew.dp import run_dp
    from app.domain import SceneBreakdown
    from app.video import get_registry

    _t("⑤ 摄影指导：逐镜提示词 …")
    prov_label, max_dur = "", 0.0
    if video_rows:
        prov = get_registry().get_provider(
            video_rows[0].name, api_key=video_rows[0].api_key,
            base_url=video_rows[0].base_url)
        if prov:
            prov_label, max_dur = prov.label, prov.max_duration_sec
    bds = [SceneBreakdown.model_validate(b) for b in
           ([bd.model_dump() for bd in breakdowns])]
    res = run_dp(llm, script, breakdowns=bds, style_guide=guide,
                 target_provider=video_rows[0].name if video_rows else "",
                 provider_label=prov_label, prompt_language=lang,
                 max_duration=max_dur)
    if not res.success:
        _t(f"!! 摄影失败：{res.summary}")
        return 1
    shots_draft = [{**s.model_dump(), "prompt_status": "needs_review",
                    "prompt_review_note": ""} for s in res.data]
    pv = st.create_video_version(
        project_id=p.id, shots=shots_draft, source_type="agent",
        label="脚本直跑·提示词待批准", notes=res.summary,
        parent_version_id=dv.id,
    )
    _t(f"   完成：{len(shots_draft)} 镜提示词已出草稿")

    # ---- 7. 批准（脚本代批：复刻批量审阅端点的写入路径） ----
    from app.api.video import _style_context
    from app.crew.dramaturgy import refresh_prompt_audits

    shots = [dict(s) for s in pv.shots if isinstance(s, dict)]
    # 分批试跑：只批准前 N 镜（批次只提交 approved 的，其余保持 needs_review）。
    limit = args.shot_limit if args.shot_limit > 0 else len(shots)
    n_appr = 0
    for s in shots:
        if s.get("video_prompt") and n_appr < limit:
            s["prompt_status"] = "approved"
            n_appr += 1
    refresh_prompt_audits(shots, style_context=_style_context(pv))
    approved_v = st.create_video_version(
        project_id=p.id, shots=shots, style_guide=pv.style_guide,
        source_type="manual", label="脚本直跑·批量批准",
        notes="无人值守脚本代批（产品内仍是人工闸门 D-43）",
        parent_version_id=pv.id,
    )
    n_ok = sum(1 for s in shots if s.get("prompt_status") == "approved")
    _t(f"⑥ 已批准 {n_ok}/{len(shots)} 镜（含 dramaturgy 审计刷新）")

    # ---- 8. 批量出片（串行 + 接力 + 质检重 roll） ----
    from app.video.service import BatchCreateRequest, generate_batch

    _t("⑦ 提交出片批次（MiniMax H3，串行推进） …")
    batch = generate_batch(p.id, BatchCreateRequest(
        mode="auto", version_id=approved_v.id))
    _t(f"   批次 {batch['id']} 启动：{batch['total']} 镜")

    t0 = time.time()
    last_done = 0
    while True:
        b = batch_manager().latest_for_project(p.id)
        if b is None:
            break
        if b.status in ("done", "failed", "aborted"):
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

    # ---- 9. 成片合成（拼接 + 配音 + 字幕） ----
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
        for w in info["warnings"]:
            _t(f"   ⚠ {w}")

    # ---- 10. 导出：成片字节从 DB 落盘 ----
    asset = st.get_asset(info["video_asset_id"])
    if asset is None or not asset.content:
        _t("!! 成片资产缺失，无法导出")
        return 1
    out = Path(args.out) if args.out else ROOT / "output" / f"{title}_成片.mp4"
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(asset.content)
    _t(f"✅ 成片已导出：{out}（{out.stat().st_size/1e6:.1f} MB）")
    _t(f"   作品 id={p.id}，可在界面里回看台本/分镜/提示词/版本链")
    return 0


if __name__ == "__main__":
    sys.exit(main())
