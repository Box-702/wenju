# =====================================================================
# inline.py —— 把本地素材变成「服务商能直接消费」的形态
#
# 背景（2026-11-09 实测 MiniMax H3 V2 接口，见 docs/DEVELOPMENT.md 5.29）：
#   - content[].video_url.url / image_url.url 只接受 http(s):// 或 data:...;base64。
#     本地回放地址（/api/assets/file/xxx）是相对路径，服务商拉不到 → 报
#     "video url must be http(s):// or data:...;base64"。
#   - files/upload 没有视频用途的 purpose（只有 voice_clone），file_id 也塞不进
#     video_url —— 想把本地文件喂给服务商，唯一可行形态是 base64 data URI。
#   - 实测 data:video/mp4;base64 能正常出片；30MB 的 body 服务商也收。
#
# 所以：参考素材（本地导入的视频/图片）在提交给服务商前，内联成 data URI。
# 公网直链（成片直链、上一镜接力）原样透传——那是服务商自己能拉的。
#
# 体积上限：base64 会膨胀 1/3，且服务商对参考视频时长有 2–15 秒的要求
# （实测超 15 秒报 "video duration 21167 ms, expected [2000, 15000] ms"）。
# 超限时宁可少带一条参考，也不要让整批出片失败。
# =====================================================================

from __future__ import annotations

import base64
import logging
from typing import Any

log = logging.getLogger(__name__)

# 内联上限（原始字节）。base64 后膨胀 ~1.3 倍，30MB 的 body 实测能收，
# 这里留一档余量：25MB 原始 ≈ 33MB body。
MAX_INLINE_BYTES = 25 * 1024 * 1024

# 单次任务的内联总量上限（原始字节）。一次请求里所有参考素材加起来不能
# 无限膨胀——最多 9 张参考图 + 3 段参考视频，全内联就得上百 MB。
MAX_TOTAL_INLINE_BYTES = 60 * 1024 * 1024

_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}


def _mime_of(name: str, kind: str) -> str:
    """按文件名后缀推断 MIME；认不出来就按 kind 兜底。"""
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    mime = _MIME_BY_EXT.get(suffix)
    if mime:
        return mime
    return {"video": "video/mp4", "image": "image/png", "audio": "audio/mpeg"}.get(kind, "application/octet-stream")


def _asset_bytes(asset: Any) -> bytes | None:
    """取素材本体：数据库字节（content）是唯一的真相来源。"""
    content = getattr(asset, "content", None)
    return bytes(content) if content else None


def inline_asset_url(store: Any, url: str) -> str | None:
    """把一个素材地址变成服务商能消费的地址。

    - 公网直链（http/https）→ 原样返回（服务商自己能拉）。
    - 本地回放地址（/api/assets/file/xxx）→ 读出字节，内联成 data URI。
    - 已经是 data URI（图片库里的定妆图就是这种）→ 原样返回。
    - 认不出来 / 本体缺失 / 太大 → None（调用方跳过这一条，别让整批失败）。
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://", "data:")):
        return raw
    if not raw.startswith("/api/assets/file/"):
        return None

    asset_id = raw.rsplit("/", 1)[-1]
    asset = store.get_asset(asset_id)
    if asset is None:
        log.warning("参考素材不存在，跳过：%s", raw)
        return None
    data = _asset_bytes(asset)
    if not data:
        log.warning("参考素材没有可读内容，跳过：%s", raw)
        return None
    if len(data) > MAX_INLINE_BYTES:
        log.warning(
            "参考素材超过内联上限（%.1fMB > %.1fMB），跳过：%s",
            len(data) / 1048576, MAX_INLINE_BYTES / 1048576, raw,
        )
        return None
    mime = _mime_of(getattr(asset, "name", "") or "", getattr(asset, "kind", "") or "")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def inline_media_urls(store: Any, urls: list[str]) -> list[str]:
    """批量内联；跳过不了的条目直接丢弃（宁可少带一条，也不让整批出片失败）。

    总量有上限：一次请求里所有参考素材加起来不能无限膨胀。
    """
    out: list[str] = []
    used = 0
    for url in urls or []:
        inlined = inline_asset_url(store, url)
        if not inlined:
            continue
        size = len(inlined) * 3 // 4  # data URI 里 base64 还原回的原始字节数
        if used + size > MAX_TOTAL_INLINE_BYTES:
            log.warning(
                "参考素材内联总量超过上限（%.1fMB），跳过：%s",
                used / 1048576, url[:80],
            )
            continue
        used += size
        out.append(inlined)
    return out


# ---- 接力参考视频的尾部裁剪（计费优化，2026-11-12 账单复盘） ----
# MiniMax 计费公式 total_seconds = input_seconds + output_seconds：
# 参考视频按「秒」与输出同价。整条 4-6s 前镜成片喂进去，输入比输出还贵
# （实测 ¥8.3/镜里 ¥4.5 是输入视频）。衔接锚真正需要的是前镜**结尾**
# （尾帧接首帧的物理意义），裁到尾部 2.5s 足够，成本直接砍半。
# 官方下限 2s，别裁更短。

RELAY_TAIL_SEC = 2.5


def clip_last_frame_uri(url: str, *, timeout: float = 90.0) -> str | None:
    """下载公网成片 → ffmpeg 抽**最后一帧** → PNG data URI。失败返回 None。

    接力锚的更优形态（2026-11-13 seedance 借鉴清单 #1 落地）：
    - reference_image 每任务前 5 张免费，尾帧图 ≈ 0 成本；裁尾视频按输入秒计费；
    - 视频是**运镜模仿**信号（厂商语义），两条不同机位的接力视频会互相打架
      （果园 v2「中段偏移」嫌疑）；一张静帧只锚身份/环境/光线，不塞运动指令，
      运镜交回提示词文字（[Shot N] 语法 + camera_path 句式）；
    - 与 re-anchor 纪律同向：产物链只做空间衔接，不做身份传递。
    """
    if not url.startswith(("http://", "https://")):
        return None
    try:
        import base64
        import subprocess
        import tempfile
        from pathlib import Path

        import httpx

        from ..video.assemble import find_ffmpeg

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return None
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
            resp = client.get(url)
            resp.raise_for_status()
            raw = resp.content
        if len(raw) > MAX_INLINE_BYTES:
            return None
        with tempfile.TemporaryDirectory(prefix="relay_frame_") as tmp:
            src = Path(tmp) / "in.mp4"
            dst = Path(tmp) / "last.png"
            src.write_bytes(raw)
            # -sseof -0.25：末尾 0.25s 内取帧；-update 1 只留最后一张
            r = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-sseof", "-0.25", "-i", str(src),
                 "-update", "1", "-frames:v", "1", "-q:v", "2", str(dst)],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0 or not dst.exists():
                log.warning("尾帧抽取失败（回退裁尾视频）：%s", r.stderr[:160])
                return None
            data = dst.read_bytes()
            if len(data) > MAX_INLINE_BYTES:
                return None
            return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
    except Exception as e:  # noqa: BLE001
        log.warning("尾帧抽取异常（回退裁尾视频）：%s", e)
        return None


def clip_tail_data_uri(url: str, *, tail_sec: float = RELAY_TAIL_SEC,
                       timeout: float = 90.0) -> str | None:
    """下载公网成片 → ffmpeg 裁出尾部 → data URI。失败返回 None（调用方回退整条）。

    裁完的片段（480P 2.5s ≈ 1MB）走 base64 内联——服务商拉不到我们的本地
    文件，而原直链虽然它能拉，但按整条秒数计费。data URI 的秒数就是裁后的。"""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        import subprocess
        import tempfile
        from pathlib import Path

        import httpx

        from ..video.assemble import find_ffmpeg, find_ffprobe

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return None
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
            resp = client.get(url)
            resp.raise_for_status()
            raw = resp.content
        if len(raw) > MAX_INLINE_BYTES:
            return None
        with tempfile.TemporaryDirectory(prefix="relay_tail_") as tmp:
            src = Path(tmp) / "in.mp4"
            dst = Path(tmp) / "out.mp4"
            src.write_bytes(raw)
            # 探时长：ffprobe 优先，缺则退回 ffmpeg -i 的 stderr 解析
            total = 0.0
            probe = find_ffprobe()
            if probe:
                pr = subprocess.run(
                    [probe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
                    capture_output=True, text=True, timeout=30,
                )
                try:
                    total = float(pr.stdout.strip())
                except ValueError:
                    total = 0.0
            if total <= 0:
                return None
            if total <= tail_sec + 0.3:
                # 本来就短于目标：省一次转码，直接内联原片
                return f"data:video/mp4;base64,{base64.b64encode(raw).decode('ascii')}"
            start = max(0.0, total - tail_sec)
            r = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(src),
                 "-t", f"{tail_sec:.2f}", "-c:v", "libx264", "-preset", "veryfast",
                 "-an", "-pix_fmt", "yuv420p", str(dst)],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0 or not dst.exists():
                log.warning("接力尾部裁剪失败（回退整条）：%s", r.stderr[:160])
                return None
            data = dst.read_bytes()
            if len(data) > MAX_INLINE_BYTES:
                return None
            return f"data:video/mp4;base64,{base64.b64encode(data).decode('ascii')}"
    except Exception as e:  # noqa: BLE001
        log.warning("接力尾部裁剪异常（回退整条）：%s", e)
        return None
