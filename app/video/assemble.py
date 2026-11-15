# =====================================================================
# assemble.py —— 视频拼接（FFmpeg）
#
# 将多个视频片段按镜头顺序拼接成完整短剧。
# 使用 FFmpeg 的 concat demuxer 实现无重编码拼接（速度最快）。
# 如果片段编码不一致，回退到 filter_complex 重编码。
# =====================================================================

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# xfade 支持的转场名（ffmpeg 4.3+）。产品只暴露 cut / fade / dissolve 三种，
# 这里列全是为了让「不认识的名字」有确定的降级去处（→ fade）而不是报错。
_XFADE_TRANSITIONS = {
    "fade", "fadeblack", "fadewhite", "fadegrays", "dissolve", "pixelize",
    "radial", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft",
    "slideright", "slideup", "slidedown", "circlecrop", "rectcrop", "distance",
    "diagtl", "diagtr", "diagbl", "diagbr", "hlslice", "hrslice", "vuslice",
    "vdslice", "hblur", "zoomin",
}


def _probe_duration(path: str) -> float:
    """探一个视频片段的时长（秒）。探不到返回 0。

    xfade 要靠 offset 决定转场发生在第几秒，没有时长就没法算——所以
    「探不到」必须显式返回 0，让调用方退回硬拼，而不是拿 0 当真值拼出黑屏。
    """
    try:
        info = get_video_info(path)
        return float(info.get("duration") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def find_ffmpeg() -> str | None:
    """查找 FFmpeg：PATH 优先；都没有时用 imageio-ffmpeg 自带的二进制兜底，
    保证没装 FFmpeg 的新机器拼接/抽帧也能直接跑。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001  依赖未安装或平台不支持
        return None


def find_ffprobe() -> str | None:
    """查找 FFprobe 可执行文件路径。"""
    return shutil.which("ffprobe")


def get_video_info(path: str, ffmpeg_path: str | None = None) -> dict[str, Any]:
    """获取视频文件信息（时长、分辨率、编码）。"""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return {"error": "ffprobe 未找到"}

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        import json
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        # r_frame_rate 形如 "30/1"：以前用 eval() 算这个分数——对用户可影响的
        # 数据做 eval 是任意代码执行原语（2026-11-11 权限审计），改成显式解析。
        fps = 0.0
        rate = str(video_stream.get("r_frame_rate") or "")
        if "/" in rate:
            num, _, den = rate.partition("/")
            try:
                d = float(den)
                if d > 0:
                    fps = float(num) / d
            except ValueError:
                fps = 0.0
        return {
            "duration": float(data.get("format", {}).get("duration", 0)),
            "width": video_stream.get("width", 0),
            "height": video_stream.get("height", 0),
            "codec": video_stream.get("codec_name", ""),
            "fps": fps,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def assemble_videos(
    video_paths: list[str],
    output_path: str,
    *,
    transition: str = "cut",
    crossfade_duration: float = 0.5,
) -> dict[str, Any]:
    """将多个视频片段拼接成一个完整视频。

    Args:
        video_paths: 视频文件路径列表（按顺序）。
        output_path: 输出文件路径。
        transition: 转场方式（cut / fade / dissolve）。
        crossfade_duration: 转场时长（秒）。

    Returns:
        {"ok": True, "output": path, "duration": seconds} 或 {"ok": False, "error": msg}
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return {"ok": False, "error": "FFmpeg 未找到，请安装 FFmpeg 后重试"}

    if not video_paths:
        return {"ok": False, "error": "没有视频片段"}

    if len(video_paths) == 1:
        # 单个片段直接复制
        shutil.copy2(video_paths[0], output_path)
        info = get_video_info(output_path)
        return {"ok": True, "output": output_path, "duration": info.get("duration", 0)}

    # 尝试 concat demuxer（无重编码，最快）
    result = _concat_demuxer(ffmpeg, video_paths, output_path)
    if result["ok"]:
        info = get_video_info(output_path)
        result["duration"] = info.get("duration", 0)
        return result

    # 回退到 filter_complex（重编码）
    log.info("concat demuxer 失败，回退到 filter_complex：%s", result.get("error"))
    result = _filter_complex(ffmpeg, video_paths, output_path, transition, crossfade_duration)
    if result["ok"]:
        info = get_video_info(output_path)
        result["duration"] = info.get("duration", 0)
    return result


def _concat_demuxer(ffmpeg: str, video_paths: list[str], output_path: str) -> dict[str, Any]:
    """使用 concat demuxer 拼接（要求所有片段编码格式一致）。"""
    try:
        # 创建 concat 文件列表
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            concat_file = f.name
            for vp in video_paths:
                # 路径中的单引号需要转义
                safe_path = vp.replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")

        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file,
                "-c", "copy",
                output_path,
            ],
            capture_output=True, text=True, timeout=300,
        )

        Path(concat_file).unlink(missing_ok=True)

        if result.returncode == 0:
            return {"ok": True, "output": output_path}
        return {"ok": False, "error": result.stderr[-500:] if result.stderr else "未知错误"}

    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _filter_complex(
    ffmpeg: str,
    video_paths: list[str],
    output_path: str,
    transition: str,
    crossfade_duration: float,
) -> dict[str, Any]:
    """使用 filter_complex 拼接（支持转场，但需要重编码）。"""
    try:
        cmd = [ffmpeg, "-y"]

        # 输入所有视频
        for vp in video_paths:
            cmd.extend(["-i", vp])

        n = len(video_paths)
        mode = (transition or "cut").strip().lower()

        if mode == "cut" or n <= 1:
            # 简单拼接
            filter_parts = []
            for i in range(n):
                filter_parts.append(f"[{i}:v:0][{i}:a:0]")
            filter_str = "".join(filter_parts) + f"concat=n={n}:v=1:a=1[outv][outa]"
            cmd.extend([
                "-filter_complex", filter_str,
                "-map", "[outv]", "-map", "[outa]",
            ])
        else:
            # 真转场：视频走 xfade 链、音频走 acrossfade 链。
            #
            # 此前的「带转场」分支产出的 filter 与 cut 分支**一模一样**
            # （都是纯 concat），注释承诺的「全局 fade」根本没加；
            # crossfade_duration 传进来也从没被用过——转场是个空壳。
            # xfade 要求知道每个片段的时长才能算 offset（转场发生在第几秒），
            # 探不到时长就退回硬拼——宁可没有转场，也不要拼出黑屏。
            durations = [_probe_duration(vp) for vp in video_paths]
            if not durations or any(d <= 0 for d in durations):
                log.warning("转场拼接：片段时长探不到，退回硬拼（无转场）")
                filter_parts = []
                for i in range(n):
                    filter_parts.append(f"[{i}:v:0][{i}:a:0]")
                filter_str = "".join(filter_parts) + f"concat=n={n}:v=1:a=1[outv][outa]"
                cmd.extend([
                    "-filter_complex", filter_str,
                    "-map", "[outv]", "-map", "[outa]",
                ])
            else:
                dur = max(0.05, float(crossfade_duration or 0.5))
                xname = mode if mode in _XFADE_TRANSITIONS else "fade"
                vfilters: list[str] = []
                afilters: list[str] = []
                prev_v, prev_a = "0:v", "0:a"
                elapsed = durations[0]
                for i in range(1, n):
                    offset = max(0.0, elapsed - dur)
                    out_v, out_a = f"vx{i}", f"ax{i}"
                    vfilters.append(
                        f"[{prev_v}][{i}:v]xfade=transition={xname}"
                        f":duration={dur:.3f}:offset={offset:.3f}[{out_v}]")
                    afilters.append(
                        f"[{prev_a}][{i}:a]acrossfade=d={dur:.3f}:c1=tri:c2=tri[{out_a}]")
                    prev_v, prev_a = out_v, out_a
                    elapsed = offset + durations[i]
                cmd.extend([
                    "-filter_complex", ";".join(vfilters + afilters),
                    "-map", f"[{prev_v}]", "-map", f"[{prev_a}]",
                ])

        cmd.extend([
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            output_path,
        ])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode == 0:
            return {"ok": True, "output": output_path}
        return {"ok": False, "error": result.stderr[-500:] if result.stderr else "未知错误"}

    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
