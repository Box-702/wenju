# =====================================================================
# orchard_relay.py —— 果园对白重跑：验证「归一化 + 跨单元接力」是否治好漂移
#
# 背景（DEVELOPMENT.md 5.40）：上一轮 demo 三个单元全部零参考裸跑，
# 单元 2 起换脸换景。本轮改动后重跑，判定点：
#   ① 提交参数里单元 2/3 应带 reference_videos（前段成片裁尾）；
#   ② 提示词全文自带全局锚（果园/少女/绅士不再只活在第一段）；
#   ③ 观感：单元 2 起场景/人物是否延续。
# 全程走真实出片链路（付费 ~¥6，Lite 768P）。新建独立作品，
# 不与旧 demo 共享 shot id（旧成片会被接力逻辑误认成锚点）。
# =====================================================================
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UNITS = [
    {"prompt": "果园午后，过肩镜头：前景虚焦男性肩膀，焦点锁在20岁西方少女面部，"
               "她缓慢踱步打量对方，眼眶泛红，哽咽说：{Am I a machine without feelings?} "
               "说完停顿吸气。黄金侧逆光勾勒发丝轮廓，手持轻微晃动。",
     "duration_sec": 9,
     "shots": [
         {"duration_sec": 5, "shot_type": "over_shoulder", "subject": "20岁西方少女", "action": "踱步打量"},
         {"duration_sec": 4, "shot_type": "close_up", "subject": "20岁西方少女", "action": "哽咽质问"}]},
    {"prompt": "At 00:09, cuts to 30岁中世纪绅士正面近景，无前景遮挡：他听完质问眉头微蹙、"
               "下颌收紧，沉默倾听，喉结滚动，胸腔平缓起伏。",
     "duration_sec": 4},
    {"prompt": "At 00:13, cuts back 少女过肩镜头：情绪转坚定，直视对方缓慢顿挫念："
               "{I have as much soul as you, and full as much heart!} 停顿换气，继续念后半段。"
               "侧逆光，自然眨眼。",
     "duration_sec": 12,
     "shots": [
         {"duration_sec": 6, "shot_type": "over_shoulder", "subject": "20岁西方少女", "action": "坚定质问"},
         {"duration_sec": 6, "shot_type": "close_up", "subject": "20岁西方少女", "action": "顿挫念白"}]},
]

LOG = Path(__file__).resolve().parent / "orchard_relay.log"
_fh = LOG.open("w", encoding="utf-8-sig")


def _t(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _fh.write(line + "\n")
    _fh.flush()


def main() -> int:
    from app.config import bootstrap_env, get_settings

    bootstrap_env()
    from app.deps import batch_manager, settings, store
    from app.llm.seed import seed_keys_from_env

    st = store()
    seed_keys_from_env(get_settings(), st)

    from app.main import _wire_video_batch, _wire_video_job_writeback, _wire_video_qa

    _wire_video_job_writeback()
    _wire_video_qa()
    _wire_video_batch()
    from app.video.sweeper import start_sweeper

    start_sweeper(run_now=True)

    # ---- 1. 导入（走工具本体 = 归一化的真实路径）----
    from app.chat.tools import production
    from app.chat.tools.context import ToolContext

    collector = {"project_id": None, "payloads": []}
    ctx = ToolContext(store=st, llm=None, collector=collector)
    tools = {t.name: t for t in production.build(ctx)}
    r = tools["import_storyboard"].invoke({
        "units": json.dumps(UNITS, ensure_ascii=False),
        "title": "果园对白·接力v2",
    })
    _t(f"① 导入：{r}")
    pid = collector["project_id"]
    import re as _re

    version_id = _re.search(r"id=(vver_[0-9a-f]+)", r).group(1)

    # ---- 2. 批准全部单元头（脚本代批；提交名单只看单元头批准状态）----
    ver = st.get_video_version(version_id)
    shots = [dict(s) for s in ver.shots if isinstance(s, dict)]
    head_ids = set()
    for s in shots:
        if not s.get("group_id") or int(s.get("group_index") or 0) == 0:
            s["prompt_status"] = "approved"
            head_ids.add(s["id"])
    approved_v = st.create_video_version(
        project_id=pid, shots=shots, source_type="manual",
        label="接力验证·批准", notes="脚本代批（验证轮）",
        parent_version_id=version_id,
    )
    _t(f"② 批准 {len(head_ids)} 个单元头 → {approved_v.id}")

    # ---- 3. 批次出片（串行推进：接力在提交时由 _auto_references 挂上）----
    from app.video.service import BatchCreateRequest, generate_batch

    batch = generate_batch(pid, BatchCreateRequest(mode="auto", version_id=approved_v.id))
    _t(f"③ 批次 {batch['id']} 启动：{batch['total']} 个生成单元")

    t0, last = time.time(), ""
    while True:
        b = batch_manager().latest_for_project(pid)
        if b is None or b.status in ("done", "failed", "aborted"):
            break
        snap = f"{len(b.results)}/{len(b.shot_ids)} 完成，当前 {b.current_shot_id or '—'}"
        if snap != last:
            _t(f"   {snap}（{time.time() - t0:.0f}s）")
            last = snap
        time.sleep(10)
    b = batch_manager().latest_for_project(pid)
    _t(f"④ 批次结束：{b.status if b else '—'}")

    # ---- 4. 关键验证：接力真的挂上了吗（打印每个 job 的参考参数）----
    jobs = sorted(st.list_video_jobs(pid), key=lambda j: j.created_at)
    total_cost = 0.0
    problems: list[str] = []
    first_head = sorted(head_ids)[0]
    for j in jobs:
        p = j.params or {}
        rv = p.get("reference_videos") or []
        ri = p.get("reference_images") or []
        _t(f"   job {j.id} shot={j.shot_id} {j.status} "
           f"时长={p.get('duration_sec')}s 参考视频×{len(rv)} 参考图×{len(ri)} "
           f"估算={j.cost_estimate}")
        if j.cost_estimate:
            total_cost += float(j.cost_estimate)
        if j.status == "succeeded":
            if j.shot_id != first_head and not rv:
                problems.append(f"{j.shot_id} 没挂接力参考")
            if j.shot_id == first_head and rv:
                problems.append(f"{j.shot_id} 首单元不该有接力（哪来的？）")
    _t(f"⑤ 费用合计估算 ≈ ¥{total_cost:.2f}")
    _t("⑤ 接力挂载：" + ("OK（非首单元全部带前段成片参考）" if not problems
       else "!! " + "; ".join(problems)))

    # ---- 5. 合成 ----
    from app.video.finalize import finalize_film

    _t("⑥ 成片合成（原生声音模式）…")
    info = finalize_film(pid, version_id=approved_v.id, subtitles=False,
                         audio_mode="native", on_progress=lambda m: _t(f"   · {m}"))
    _t(f"   成片：{info.get('video_url')} · {info.get('duration_sec')}s · "
       f"{info.get('shots_used')}/{info.get('shots_total')} 镜")
    return 0


if __name__ == "__main__":
    sys.exit(main())
