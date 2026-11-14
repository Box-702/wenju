# =====================================================================
# ab_dialect.py —— 切镜语法统一性实验（2026-11-14）
#
# 三发任务（全部走真接口，预算 ≈¥32）：
#   A) Seedance 2.5 探针：4s 最小任务，校模型 id/字段/时长区间；
#   B) 2.0 + 「镜头N（秒数）：」方言（现行 seedance 折叠语法）；
#   C) 2.0 + 「[Shot N] At 00:XX, cuts to」方言（H3/2.5 语法）。
# 判定点：
#   1. C 的画面里有没有时间戳文字渲染（角标/怪字幕）——「写了无害」假设的检验；
#   2. B/C 切点谁更贴预算（4/10/14s）；
#   3. A 的响应字段与 2.0 是否同构（决定适配器要不要分支）。
# 产物：N2D/output/ab_*.mp4（本地审阅用临时件，看完即删）。
# =====================================================================
import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _runs_dir():
    """实验产物落产品主目录（D-44），家目录不可用才退回 N2D/output。"""
    try:
        from app.home import ensure_home, runs_root

        ensure_home(ROOT)
        d = runs_root() / "ab_dialect_1114"
    except Exception:  # noqa: BLE001
        d = ROOT / "N2D" / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


GROUP = (_runs_dir() / "group_prompt.txt").read_text(encoding="utf-8")
OUT = _runs_dir()


def to_timestamp_dialect(text: str) -> str:
    """镜头N（D秒）：→ [Shot N] At MM:SS.mmm, cuts to…（时间戳按累加算）。"""
    parts = re.split(r"(镜头\d+（\d+秒）：)", text)
    out, cursor = [], 0.0
    for i in range(1, len(parts), 2):
        mark, body = parts[i], parts[i + 1]
        d = int(re.search(r"（(\d+)秒）", mark).group(1))
        n = re.search(r"镜头(\d+)", mark).group(1)
        if n == "1":
            out.append(f"[Shot 1] 0-{d}s，{body}")
        else:
            out.append(f"[Shot {n}] At {int(cursor // 60):02d}:{cursor % 60:06.3f}, cuts to {body}")
        cursor += d
    # 非切镜段（全局锚/风格尾）原样保留
    lead = parts[0]
    tail = parts[-1] if len(parts) % 2 == 0 else ""
    joined = "".join(out)
    if lead:
        joined = lead + joined
    if tail:
        joined = joined + tail
    return joined


async def main() -> int:
    from app.config import bootstrap_env

    bootstrap_env()
    from app.deps import store
    from app.video.base import VideoJobParams
    from app.video.registry import get_registry

    k = next(r.api_key for r in store().list_api_providers(kind="video")
             if r.name == "seedance")
    jobs = []

    # A) 2.5 探针
    p25 = get_registry().get_provider("seedance", api_key=k,
                                       model="doubao-seedance-2-5-260628")
    try:
        r = await p25.create_job("果园午后，过肩镜头：20岁西方少女抬头看向镜头，"
                                 "眼眶泛红，哽咽说：{Am I a machine?} 侧逆光。",
                                 VideoJobParams(duration_sec=4, resolution="720p",
                                                aspect_ratio="16:9"))
        jobs.append(("A_2.5探针", p25, r.external_task_id))
        print(f"A 创建 OK: {r.external_task_id}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"A 创建失败: {e}", flush=True)

    # B/C) 2.0 双方言（同组同参，只差切镜写法）
    p20 = get_registry().get_provider("seedance", api_key=k,
                                       model="doubao-seedance-2-0-260128")
    ts_prompt = to_timestamp_dialect(GROUP)
    print(f"\n方言C样例: {ts_prompt[ts_prompt.find('[Shot 2]'):][:90]}\n", flush=True)
    for label, prompt in (("B_镜头N方言", GROUP), ("C_时间戳方言", ts_prompt)):
        r = await p20.create_job(prompt, VideoJobParams(
            duration_sec=14, resolution="720p", aspect_ratio="16:9"))
        jobs.append((label, p20, r.external_task_id))
        print(f"{label} 创建 OK: {r.external_task_id}", flush=True)

    # 轮询到全终态
    results = {}
    t0 = time.time()
    pending = {label: (prov, tid) for label, prov, tid in jobs}
    while pending and time.time() - t0 < 1500:
        await asyncio.sleep(20)
        for label, (prov, tid) in list(pending.items()):
            st = await prov.poll_job(tid)
            if st.status in ("succeeded", "failed", "cancelled"):
                results[label] = (st.status, st.video_url or st.error_message)
                del pending[label]
                print(f"[{time.time()-t0:3.0f}s] {label}: {st.status}", flush=True)
    for label, (status, url) in results.items():
        if status == "succeeded" and url.startswith("http"):
            import httpx

            dest = OUT / f"ab_{label}.mp4"
            dest.write_bytes(httpx.get(url, timeout=120).content)
            print(f"下载 {dest.name} ({dest.stat().st_size//1048576} MB)")
        else:
            print(f"{label}: {status} {str(url)[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
