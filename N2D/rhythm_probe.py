# =====================================================================
# rhythm_probe.py —— 节奏探针：只跑「剧本 → 分镜」，不建项目、不出图、不出片
#
# 目的：在改改编/导演规则前后，用同一份原文量同一组指标——
#   场数 / 节拍数 / 台词量 / 估算时长 / 镜头数 / 每场时长 / 重复镜 / 钩子。
#
# 用法（项目根目录）：
#   uv run python N2D/rhythm_probe.py 素材/劳山道士.txt --title 劳山道士
#   uv run python N2D/rhythm_probe.py 素材/劳山道士.txt --no-director  # 只跑剧本
#
# 只调对话模型（要花钱），不碰视频供应商；不写 projects / script_versions，
# 指标落 ~/.wenju/runs/rhythm_probe_<title>_<时间>/（report.md + metrics.json）。
# =====================================================================

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from app.home import ensure_home, runs_root

    ensure_home(ROOT)
    RUNS = runs_root()
except Exception:  # noqa: BLE001
    RUNS = Path(__file__).resolve().parent / "output"

ACTION_BEAT_SEC = 2.5   # 动作拍的经验时值（与改编/导演侧的估算口径一致）


def _dialogue_seconds(script) -> float:
    from app.video.rhythm import speech_need_seconds

    total = 0.0
    for sc in script.scenes:
        text = "".join((b.line or "") for b in sc.beats if b.type == "dialogue")
        if text.strip():
            total += speech_need_seconds(text)
    return total


def _script_metrics(script) -> dict:
    beats = sum(len(sc.beats) for sc in script.scenes)
    action_beats = sum(1 for sc in script.scenes for b in sc.beats if b.type == "action")
    dialogue_beats = sum(1 for sc in script.scenes for b in sc.beats if b.type == "dialogue")
    dialogue_chars = sum(len(b.line or "") for sc in script.scenes for b in sc.beats
                         if b.type == "dialogue")
    dlg_sec = _dialogue_seconds(script)
    est_sec = dlg_sec + action_beats * ACTION_BEAT_SEC
    plan = getattr(script, "adaptation_plan", None)
    scenes = []
    for sc in script.scenes:
        sc_beats = len(sc.beats)
        sc_action = sum(1 for b in sc.beats if b.type == "action")
        sc_text = "".join((b.line or "") for b in sc.beats if b.type == "dialogue")
        sc_dlg_sec = 0.0
        if sc_text.strip():
            from app.video.rhythm import speech_need_seconds
            sc_dlg_sec = speech_need_seconds(sc_text)
        scenes.append({
            "id": sc.id,
            "title": sc.title,
            "beats": sc_beats,
            "action_beats": sc_action,
            "dialogue_beats": sum(1 for b in sc.beats if b.type == "dialogue"),
            "dialogue_chars": len(sc_text),
            "estimate_sec": round(sc_dlg_sec + sc_action * ACTION_BEAT_SEC, 1),
            "target_sec": (plan.scene_targets.get(sc.id) if plan else None),
        })
    plan_summary = None
    if plan is not None:
        segs = plan.segments
        plan_summary = {
            "target_sec": plan.target_sec,
            "episode_count": plan.episode_count,
            "segments": len(segs),
            "must_keep": sum(1 for s in segs if s.plot_importance == "must_keep"),
            "expand": sum(1 for s in segs if s.treatment == "expand"),
            "compress": sum(1 for s in segs if s.treatment == "compress"),
            "cut": sum(1 for s in segs if s.treatment == "cut"),
            "hook": plan.hook,
            "segment_rows": [
                {"id": s.id, "importance": s.plot_importance, "treatment": s.treatment,
                 "function": s.function, "focus": s.character_focus,
                 "beats": s.target_beats, "sec": s.target_sec,
                 "risk": s.completeness_risk, "reason": s.reason}
                for s in segs
            ],
        }
    return {
        "scenes": len(script.scenes),
        "beats": beats,
        "action_beats": action_beats,
        "dialogue_beats": dialogue_beats,
        "dialogue_chars": dialogue_chars,
        "dialogue_sec": round(dlg_sec, 1),
        "estimate_sec": round(est_sec, 1),
        "plan": plan_summary,
        "scene_rows": scenes,
    }


def _shot_metrics(breakdowns, script) -> dict:
    from app.crew.director import _MOVEMENT_RE

    shots = [s for bd in breakdowns for s in bd.shots]
    total = sum(float(s.duration_sec or 0) for s in shots)
    per_scene = []
    for bd in breakdowns:
        per_scene.append({
            "id": bd.scene_id,
            "shots": len(bd.shots),
            "duration_sec": round(sum(float(s.duration_sec or 0) for s in bd.shots), 1),
        })
    dup = 0
    for a, b in zip(shots, shots[1:]):
        same_subject = (a.subject or "").strip() == (b.subject or "").strip() and bool(a.subject)
        same_action = (a.action or "").strip() == (b.action or "").strip() and bool(a.action)
        same_cam = (a.camera.type or "") == (b.camera.type or "")
        if same_subject and same_action and same_cam:
            dup += 1
    first = shots[0] if shots else None
    hook = False
    if first is not None:
        hook = bool(first.dialogue_ref) or bool(_MOVEMENT_RE.search(first.action or ""))
    return {
        "shots": len(shots),
        "duration_sec": round(total, 1),
        "per_scene": per_scene,
        "duplicate_adjacent": dup,
        "first_shot_hook": hook,
        "first_shot": (first.subject if first else ""),
    }


def _coverage(script, plan) -> dict:
    """must_keep 段落是否落进了有节拍的场。"""
    if plan is None:
        return {"must_keep": 0, "missing": []}
    assigned: dict[str, list[str]] = {}
    for scene_id, seg_ids in (plan.scene_segments or {}).items():
        for seg in seg_ids:
            assigned.setdefault(seg, []).append(scene_id)
    beats_by_scene = {sc.id: len(sc.beats) for sc in script.scenes}
    missing = []
    for seg in plan.segments:
        if seg.plot_importance != "must_keep":
            continue
        rooms = assigned.get(seg.id) or []
        if not rooms or all(beats_by_scene.get(r, 0) == 0 for r in rooms):
            missing.append(seg.id)
    return {"must_keep": sum(1 for s in plan.segments if s.plot_importance == "must_keep"),
            "missing": missing}


def _render(title: str, source_chars: int, sm: dict, bm: dict, cov: dict) -> str:
    lines = [f"# 节奏探针：{title}", ""]
    lines.append(f"- 源文：{source_chars} 字")
    lines.append(f"- 剧本：{sm['scenes']} 场 · {sm['beats']} 拍 "
                 f"（动作 {sm['action_beats']} / 台词 {sm['dialogue_beats']}，"
                 f"{sm['dialogue_chars']} 字）")
    lines.append(f"- 估算时长：约 {sm['estimate_sec']} 秒"
                 f"（台词 {sm['dialogue_sec']} 秒 + 动作拍 × {ACTION_BEAT_SEC}）")
    if sm.get("plan"):
        p = sm["plan"]
        lines.append(f"- 段落决策：{p['segments']} 段 · must_keep {p['must_keep']} · "
                     f"扩写 {p['expand']} · 压缩 {p['compress']} · 删除 {p['cut']} · "
                     f"目标 {p['target_sec']:.0f} 秒 / {p['episode_count']} 集")
        lines.append(f"- 钩子：{p['hook'] or '（未给）'}")
    if bm:
        lines.append(f"- 分镜：{bm['shots']} 镜 · {bm['duration_sec']} 秒 · "
                     f"重复相邻镜 {bm['duplicate_adjacent']} · 首镜钩子 {bm['first_shot_hook']}")
        if bm.get("director_errors"):
            lines.append(f"- 导演遗留问题：{len(bm['director_errors'])} 条（首条："
                         f"{bm['director_errors'][0][:60]}）")
    lines.append("")
    lines.append("| 场 | 标题 | 拍 | 台词字 | 估算秒 | 目标秒 | 镜数 | 分镜秒 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    shots = {r["id"]: r for r in (bm or {}).get("per_scene", [])}
    for r in sm["scene_rows"]:
        s = shots.get(r["id"], {})
        lines.append(f"| {r['id']} | {r['title']} | {r['beats']} | {r['dialogue_chars']} | "
                     f"{r['estimate_sec']} | {r['target_sec'] if r['target_sec'] is not None else '-'} | "
                     f"{s.get('shots', '-')} | {s.get('duration_sec', '-')} |")
    if cov.get("must_keep"):
        lines.append("")
        lines.append(f"- must_keep 覆盖：{cov['must_keep'] - len(cov['missing'])}/{cov['must_keep']}"
                     + (f"，缺失 {', '.join(cov['missing'])}" if cov["missing"] else ""))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="只跑剧本+分镜的节奏探针（不烧视频钱）")
    ap.add_argument("novel", help="小说原文 txt 路径")
    ap.add_argument("--title", default="", help="作品名（默认取文件名）")
    ap.add_argument("--adaptation", default="短剧", help="改编类型（默认 短剧）")
    ap.add_argument("--no-director", action="store_true", help="只跑剧本，不跑导演拆镜")
    args = ap.parse_args()

    novel = Path(args.novel)
    if not novel.is_file():
        ap.error(f"找不到原文：{novel}")
    title = args.title or novel.stem
    raw_text = novel.read_text(encoding="utf-8")
    if not raw_text.strip():
        ap.error("原文是空的")

    from app.config import bootstrap_env, get_settings

    bootstrap_env()
    from app.deps import llm as get_llm, store
    from app.llm.seed import seed_keys_from_env

    seed_keys_from_env(get_settings(), store())
    llm = get_llm()
    if not llm.available:
        print("!! 对话模型不可用（检查 DB 厂商 / .env key）", file=sys.stderr)
        return 1

    t0 = time.time()
    print(f"[探针] 剧本生成：{title}（{len(raw_text)} 字）…", flush=True)
    from app.pipeline.generation import generate_script as run_generation

    script, artifacts = run_generation(
        llm, get_settings(), title=title, raw_text=raw_text,
        adaptation_type=args.adaptation, language="zh-CN",
    )
    sm = _script_metrics(script)
    print(f"[探针] 剧本完成（{time.time() - t0:.0f}s）：{sm['scenes']} 场 · "
          f"{sm['beats']} 拍 · 估算 {sm['estimate_sec']} 秒", flush=True)

    bm: dict = {}
    if not args.no_director:
        t1 = time.time()
        print("[探针] 导演拆镜（要花对话模型的钱）…", flush=True)
        from app.crew.director import run_director

        res = run_director(llm, script, adaptation_type=args.adaptation)
        if not res.success:
            print(f"!! 导演失败：{res.summary}", file=sys.stderr)
        else:
            bm = _shot_metrics(res.data, script)
            bm["director_summary"] = res.summary
            bm["director_errors"] = list(res.errors or [])
            print(f"[探针] 分镜完成（{time.time() - t1:.0f}s）：{bm['shots']} 镜 · "
                  f"{bm['duration_sec']} 秒", flush=True)
            for err in bm["director_errors"][:5]:
                print(f"[探针] 导演遗留问题：{err}", flush=True)

    cov = _coverage(script, getattr(script, "adaptation_plan", None))
    report = _render(title, len(raw_text), sm, bm, cov)
    stamp = time.strftime("%m%d_%H%M%S")
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", title)[:40] or "probe"
    out_dir = RUNS / f"rhythm_probe_{safe}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report, encoding="utf-8-sig")
    (out_dir / "metrics.json").write_text(
        json.dumps({"title": title, "source_chars": len(raw_text), "script": sm,
                    "breakdown": bm, "coverage": cov,
                    "artifacts": {k: v for k, v in (artifacts or {}).items()
                                  if k in ("mode", "scene_count", "adaptation_plan")}},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(report)
    print(f"[探针] 报告：{out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
