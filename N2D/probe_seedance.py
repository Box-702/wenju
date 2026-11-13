# =====================================================================
# probe_seedance.py —— Seedance 适配器校准探针（拿真 key 打一发最小任务）
#
# 目的：seedance.py 写于无 key 阶段（2026-11-12），endpoint/模型 id/状态
# 枚举/视频 URL 字段路径都按公开文档与同族惯例实现。接进 A/B 之前必须
# 用真 key 实测一遍，把响应原文打出来对字段——对不上就改适配器，别硬跑。
#
# 用法（项目根目录，先配好 ARK_API_KEY 环境变量或 .env）：
#   uv run python N2D/probe_seedance.py
#   uv run python N2D/probe_seedance.py --model doubao-seedance-1-0-lite-t2v-250428
# 探针会真实产生一次生成费用（4 秒 480p 最小任务），跑前知悉。
# =====================================================================

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="Seedance 适配器校准探针")
    ap.add_argument("--key", default="", help="ARK API Key（缺省读 .env / 环境变量 ARK_API_KEY）")
    ap.add_argument("--model", default="", help="模型 id（缺省用适配器默认 pro 档）")
    args = ap.parse_args()

    from app.config import bootstrap_env

    bootstrap_env()
    import os

    # 与适配器同口径：ARK_API_KEY（官方名）> SEEDANCE_API_KEY（用户 .env 实际名）
    key = (args.key or os.environ.get("ARK_API_KEY", "")
           or os.environ.get("SEEDANCE_API_KEY", ""))
    if not key:
        print("!! 没有 ARK_API_KEY / SEEDANCE_API_KEY（.env 或环境变量），探针终止")
        return 1

    from app.video import get_registry
    from app.video.base import VideoJobParams

    prov = get_registry().get_provider("seedance", api_key=key,
                                       **({"model": args.model} if args.model else {}))
    if prov is None:
        print("!! registry 里没有 seedance（检查 app/video/providers/seedance.py）")
        return 1
    if args.model:
        prov.model = args.model
    print(f"provider={prov.name} model={prov.model} base={prov.base_url}")

    async def run() -> int:
        # 1) 创建：最短的一发，720p（用户指定清晰度口径；2.0 系列 duration 下限 4s）
        try:
            resp = await prov.create_job(
                "一个水墨风格的少年推开木门走进酒店，镜头缓慢推近，横屏",
                VideoJobParams(duration_sec=4, resolution="720p", aspect_ratio="16:9"),
            )
        except Exception as e:  # noqa: BLE001
            print(f"!! 创建失败：{e}")
            print("   → 常见原因：模型 id 不对（控制台看确切 id）、key 没开通视频模型、endpoint 变了")
            return 1
        print(f"创建 OK: task={resp.external_task_id} status={resp.status}")

        # 2) 轮询到终态，把最后一个原始响应完整打出来（校准字段路径用）
        t0 = time.time()
        last: dict = {}
        while time.time() - t0 < 600:
            await asyncio.sleep(8)
            st = await prov.poll_job(resp.external_task_id)
            last = st.raw
            print(f"  [{time.time()-t0:3.0f}s] status={st.status}"
                  + (f" url={st.video_url[:60]}…" if st.video_url else "")
                  + (f" err={st.error_message}" if st.error_message else ""))
            if st.status in ("succeeded", "failed", "cancelled"):
                break
        print("--- 最后一次查询原始响应（校准用，重点看 status 枚举与 video_url 路径）---")
        print(json.dumps(last, ensure_ascii=False, indent=1)[:2000])
        if st.video_url:
            print(f"\n✅ 端到端通了。下载验证：{st.video_url}")
            import httpx
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.get(st.video_url)
                print(f"   下载 HTTP {r.status_code}，{len(r.content)/1e6:.1f} MB")
            return 0
        print("\n!! 没拿到 video_url——对照上面的原始响应改适配器 poll_job 的取值路径")
        return 1

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
