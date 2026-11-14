# =====================================================================
# regen_prompts.py —— 对已有作品重跑「导演拆镜 + 摄影提示词」（只花文本钱）
#
# 用途：修了链路提示词规则后，拿旧作品重新生成一版导演拆解与逐镜提示词，
# 与旧版本并排对比质量；不提交任何视频生成（零出片费用）。
#
# 用法：uv run python N2D/regen_prompts.py <作品id> [--provider seedance]
#   产物：新 video version（source_type=agent，label 带「重生成」）
#         + ~/.wenju/runs/<作品>_重生成_<时间>/提示词对照.md
#           （D-44：用户数据不进项目文件夹；家目录不可用时退回 N2D/output/）
# =====================================================================
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _out_dir() -> Path:
    """对照文件落产品主目录（与 novel2video 同一纪律），家目录不可用才退回项目。"""
    try:
        from app.home import ensure_home, runs_root

        ensure_home(ROOT)
        return runs_root()
    except Exception:  # noqa: BLE001
        d = ROOT / "N2D" / "output"
        d.mkdir(exist_ok=True)
        return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("--provider", default="seedance")
    args = ap.parse_args()

    from app.config import bootstrap_env

    bootstrap_env()
    from app.deps import store as get_store

    st = get_store()
    p = st.get_project(args.project_id)
    if p is None:
        print(f"!! 找不到作品 {args.project_id}")
        return 1
    version = st.latest_version(p)
    if version is None:
        print("!! 该作品没有剧本版本")
        return 1
    script = version.script

    from app.crew.director import run_director
    from app.crew.dp import language_for, run_dp
    from app.domain import StyleGuide
    from app.llm import build_llm
    from app.video import get_registry

    llm = build_llm(None, get_store())  # 模型配置只走 DB（D-48），必须接 store
    style_data = st.get_video_style(p.id) or {}
    guide = StyleGuide.model_validate(style_data) if style_data else None

    rows = [r for r in st.list_api_providers(kind="video")
            if r.name == args.provider and r.enabled and r.api_key]
    prov_label, max_dur = "", 0.0
    if rows:
        prov = get_registry().get_provider(args.provider, api_key=rows[0].api_key,
                                           base_url=rows[0].base_url)
        if prov:
            prov_label, max_dur = prov.label, prov.max_duration_sec
    lang = language_for(args.provider, script.language)

    print(f"① 导演拆镜（provider={args.provider} lang={lang}）…")
    res = run_director(llm, script, adaptation_type=p.adaptation_type,
                       visual_style=style_data.get("visual_style"))
    if not res.success:
        print(f"!! 导演失败：{res.summary}")
        return 1
    breakdowns = res.data
    n_shots = sum(len(b.shots) for b in breakdowns)
    print(f"   完成：{n_shots} 镜 · {res.summary[:100]}")

    print("② 摄影指导逐镜提示词 …")
    res2 = run_dp(llm, script, breakdowns=breakdowns, style_guide=guide,
                  target_provider=args.provider, provider_label=prov_label,
                  prompt_language=lang, max_duration=max_dur)
    if not res2.success:
        print(f"!! 摄影失败：{res2.summary}")
        return 1
    shots = res2.data
    print(f"   完成：{len(shots)} 镜提示词")

    v = st.create_video_version(
        project_id=p.id, shots=[s.model_dump() for s in shots],
        source_type="agent", label=f"链路修复后重生成·{args.provider}",
        notes=res2.summary,
    )
    print(f"③ 已落新版本 {v.id}（不动旧版本，不提交出片）")

    out_dir = _out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{p.title}_重生成提示词.md"
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# 《{p.title}》链路修复后重生成提示词（{args.provider} 方言）\n\n")
        for s in shots:
            f.write(f"## {s.id}｜{s.shot_type}｜{s.camera.type}"
                    f"{'' if not s.camera.direction else '/' + s.camera.direction}"
                    f"｜{s.duration_sec:.0f}s\n\n")
            if s.camera.path:
                f.write(f"- 运镜轨迹：{s.camera.path}\n")
            f.write(f"- 审计：{'; '.join(s.prompt_audit or []) or '干净'}\n\n")
            f.write((s.video_prompt or "（无提示词）") + "\n\n")
    print(f"④ 对照文件：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
