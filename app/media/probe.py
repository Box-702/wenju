# =====================================================================
# probe.py —— 量一段媒体的时长 / 帧率 / 图片尺寸
#
# 用来在**提交前**挡住「服务商一定不收」的素材：
# MiniMax H3 V2 对参考视频有两条硬约束（都是真实跑挂换来的实测）：
#   时长 expected [2000, 15000] ms；帧率 expected [23.976, 60]——
#   低帧率录屏（15fps）会在生成阶段才报 2013，白等两分钟。
# 对参考图有尺寸下限（实测每边 [256, 5760] 像素）。用户导进参考素材的片子
# 超了就会白跑一次生成——不如在选材时就量出来，当场告诉他。
#
# 量时长/帧率优先用 ffprobe（准）；便携版常缺 ffprobe，回退解析 ffmpeg -i 的
# Duration / fps（与 app/video/qa.py 同样处理）。量不到就返回 None —— 不猜。
# =====================================================================

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")
_FPS_RE = re.compile(r"([\d.]+)\s*fps")


def _parse_rate(raw: object) -> float | None:
    """ffprobe 的帧率是分数（"30000/1001"）或小数字符串；解析不出就 None。"""
    s = str(raw or "").strip()
    if not s or s in ("N/A", "0/0"):
        return None
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f > 0 else None
        value = float(s)
        return value if value > 0 else None
    except ValueError:
        return None


def _ffmpeg_exe() -> str | None:
    """找 ffmpeg：FFMPEG_PATH 配置优先，其次 PATH，最后 imageio-ffmpeg 自带。"""
    from ..video.qa import _ffmpeg

    return _ffmpeg()


def _ffprobe_exe() -> str | None:
    """找 ffprobe：FFMPEG_PATH 同目录优先，其次 PATH。"""
    from ..video.qa import _ffprobe

    return _ffprobe()


def _with_temp_media(data: bytes, filename: str, fn):
    """把媒体本体落到临时文件跑一次外部工具，跑完即删。"""
    suffix = Path(filename or "").suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="media_probe_") as tmp:
        path = Path(tmp) / f"probe{suffix}"
        path.write_bytes(data)
        return fn(path)


def duration_seconds(data: bytes, filename: str) -> float | None:
    """量出这段媒体的时长（秒）；量不到返回 None（不当成 0，也不瞎猜）。"""
    if not data:
        return None

    def _measure(path: Path) -> float | None:
        probe = _ffprobe_exe()
        if probe:
            try:
                r = subprocess.run(
                    [probe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "json", str(path)],
                    capture_output=True, text=True, timeout=30,
                )
                value = float((json.loads(r.stdout or "{}").get("format") or {}).get("duration") or 0)
                if value > 0:
                    return value
            except Exception as e:  # noqa: BLE001
                log.debug("ffprobe 量时长失败，回退 ffmpeg：%s", e)

        ffmpeg = _ffmpeg_exe()
        if ffmpeg:
            try:
                r = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True, timeout=30)
                m = _DURATION_RE.search(r.stderr or "")
                if m:
                    h, mi, s = (float(x) for x in m.groups())
                    return h * 3600 + mi * 60 + s
            except Exception as e:  # noqa: BLE001
                log.debug("ffmpeg 量时长失败：%s", e)
        return None

    return _with_temp_media(data, filename, _measure)


def video_fps(data: bytes, filename: str) -> float | None:
    """量出视频的帧率（fps）；量不到返回 None。

    服务商要求参考视频 fps ∈ [23.976, 60]（实测 15fps 的录屏在生成阶段报
    `video fps 15.000, expected [23.976, 60]`——任务建好了才失败，白等一轮）。
    优先 ffprobe 的 avg_frame_rate（分数如 30000/1001）；回退 ffmpeg -i 的
    "23.98 fps" 文本。变帧率素材的 avg 落在窗口外也照样量个 representative。
    """
    if not data:
        return None

    def _measure(path: Path) -> float | None:
        probe = _ffprobe_exe()
        if probe:
            try:
                r = subprocess.run(
                    [probe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=avg_frame_rate,r_frame_rate",
                     "-of", "json", str(path)],
                    capture_output=True, text=True, timeout=30,
                )
                streams = json.loads(r.stdout or "{}").get("streams") or []
                if streams:
                    rate = _parse_rate(streams[0].get("avg_frame_rate")) \
                        or _parse_rate(streams[0].get("r_frame_rate"))
                    if rate:
                        return rate
            except Exception as e:  # noqa: BLE001
                log.debug("ffprobe 量帧率失败，回退 ffmpeg：%s", e)

        ffmpeg = _ffmpeg_exe()
        if ffmpeg:
            try:
                r = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True, timeout=30)
                # 取「Video: … fps」那一行里的数值（别撞上 audio 行的 bitrate）
                m = re.search(r"Video:.*?([\d.]+)\s*fps", r.stderr or "")
                if m:
                    value = float(m.group(1))
                    return value if value > 0 else None
            except Exception as e:  # noqa: BLE001
                log.debug("ffmpeg 量帧率失败：%s", e)
        return None

    return _with_temp_media(data, filename, _measure)


def image_size(data: bytes, filename: str) -> tuple[int, int] | None:
    """量图片的宽高（像素）；量不到返回 None。

    服务商对参考图有尺寸下限（实测每边至少 256 像素），入库时量出来，
    用户选材时就能当场看见，而不是提交后被服务商退回来。
    """
    if not data:
        return None

    def _measure(path: Path) -> tuple[int, int] | None:
        probe = _ffprobe_exe()
        if not probe:
            return None
        try:
            r = subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            streams = json.loads(r.stdout or "{}").get("streams") or []
            if streams:
                w = int(streams[0].get("width") or 0)
                h = int(streams[0].get("height") or 0)
                if w > 0 and h > 0:
                    return w, h
        except Exception as e:  # noqa: BLE001
            log.debug("ffprobe 量图片尺寸失败：%s", e)
        return None

    return _with_temp_media(data, filename, _measure)
