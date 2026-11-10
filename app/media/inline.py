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
