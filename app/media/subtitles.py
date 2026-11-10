# =====================================================================
# subtitles.py —— 台词字幕（时间轴 → SRT/ASS → 烧录成片）
#
# 字幕的文本与归属完全复用配音的台词归属（beats 段对齐），时间轴二选一：
#   - 配音开：直接用 TTS 实测时间轴（synthesize_track 的 start_ms/end_ms），
#     音、画、字三者天然同步；
#   - 配音关/不可用：按「字数/4 秒」在所属镜头窗口内估算（导演规则本来就
#     要求对白镜头 duration ≥ 字数/4，所以估算窗口装得下，装不下就压缩）。
#
# 输出两份文件：ASS（烧录进画面，白字黑边底部居中）+ SRT（随成片交付，
# 供平台上传/二次创作）。烧录用 ffmpeg subtitles 滤镜（libass），只需一次
# 视频重编码，音轨直拷。
# =====================================================================

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain import Script, Shot

log = logging.getLogger(__name__)

# 估算排程参数（与导演的「对白镜头时长 ≥ 字数/4 秒」规则同源）。
CHARS_PER_SEC = 4.0
EST_LINE_MIN_SEC = 0.8   # 单句最短显示时长
EST_LINE_GAP_SEC = 0.15
EST_LEAD_SEC = 0.4       # 镜头开始后多久出第一句（与配音 LINE_LEAD_SEC 一致）


@dataclass
class SubEntry:
    """一条字幕：起止时刻（成片内绝对毫秒）+ 文本。"""

    speaker: str
    text: str
    start_ms: int
    end_ms: int
    shot_id: str = ""


# ---------- 时间轴 ----------


def timeline_from_track(track: list[dict[str, Any]]) -> list[SubEntry]:
    """配音时间轴 → 字幕条目。

    兼容旧 track（只有 path/start_ms/text）：end_ms 缺失时按字数估算。
    """
    entries: list[SubEntry] = []
    for item in track or []:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        start = int(item.get("start_ms", 0))
        end = item.get("end_ms")
        if not end:
            end = start + int(max(EST_LINE_MIN_SEC, len(text) / CHARS_PER_SEC) * 1000)
        entries.append(SubEntry(
            speaker=str(item.get("speaker", "")), text=text,
            start_ms=start, end_ms=int(end), shot_id=str(item.get("shot_id", "")),
        ))
    entries.sort(key=lambda e: e.start_ms)
    return entries


def timeline_estimated(
    script: Script,
    shots: list[Shot],
    *,
    expected_shots: dict[str, int] | None = None,
) -> list[SubEntry]:
    """无配音时的估算时间轴：台词归属复用配音的段对齐，时刻按字数在镜头窗口内排布。

    窗口装不下时整组按比例压缩（单句不低于 0.5s），字幕不跨出所属镜头，
    避免和下一镜的画面内容对不上。

    ``expected_shots``：该场原本几镜。只渲染了其中一部分时，属于没拍镜头的
    台词不该出字幕（否则会出现两句字幕叠在一起、且与画面对不上）。
    """
    from .dubbing import plan_dubbing

    plan = plan_dubbing(script, shots, expected_shots=expected_shots)
    if not plan.lines:
        return []

    ordered = sorted(shots, key=lambda s: s.order)
    shot_start: dict[str, float] = {}
    shot_dur: dict[str, float] = {}
    t = 0.0
    for s in ordered:
        shot_start[s.id] = t
        shot_dur[s.id] = max(1.0, s.duration_sec)
        t += shot_dur[s.id]

    by_shot: dict[str, list] = {}
    for line in plan.lines:
        by_shot.setdefault(line.shot_id, []).append(line)

    entries: list[SubEntry] = []
    for shot_id, lines in by_shot.items():
        base = shot_start.get(shot_id, 0.0)
        window = max(1.0, shot_dur.get(shot_id, 5.0)) - EST_LEAD_SEC - 0.3  # 留尾量
        durs = [max(EST_LINE_MIN_SEC, len(ln.text) / CHARS_PER_SEC) for ln in lines]
        need = sum(durs) + EST_LINE_GAP_SEC * (len(lines) - 1)
        scale = min(1.0, window / need) if need > 0 else 1.0
        cursor = base + EST_LEAD_SEC
        for ln, d in zip(lines, durs, strict=True):
            dur = max(0.5, d * scale)
            entries.append(SubEntry(
                speaker=ln.speaker, text=ln.text,
                start_ms=int(cursor * 1000), end_ms=int((cursor + dur) * 1000),
                shot_id=shot_id,
            ))
            cursor += dur + EST_LINE_GAP_SEC * scale
    entries.sort(key=lambda e: e.start_ms)
    return entries


# ---------- 字幕文件 ----------


def _srt_ts(ms: int) -> str:
    if ms < 0:
        ms = 0
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def to_srt(entries: list[SubEntry]) -> str:
    blocks: list[str] = []
    for i, e in enumerate(entries, 1):
        blocks.append(f"{i}\n{_srt_ts(e.start_ms)} --> {_srt_ts(e.end_ms)}\n{e.text}")
    return "\n\n".join(blocks) + "\n"


def _ass_ts(ms: int) -> str:
    if ms < 0:
        ms = 0
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:d}:{m:02d}:{s:02d}.{milli // 10:02d}"


def _ass_text(text: str) -> str:
    """ASS 文本转义：{} 是覆盖标签语法，替换成全角；换行用 \\N。"""
    return (text.replace("{", "（").replace("}", "）").replace("\n", "\\N"))


def to_ass(entries: list[SubEntry], *, width: int = 1280, height: int = 720) -> str:
    """生成 ASS。libass 按 PlayRes 与实际帧尺寸自动缩放，白字黑边底部居中。"""
    fontsize = max(24, round(height * 0.066))
    outline = max(2, height // 240)
    margin_v = max(20, round(height * 0.05))
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Microsoft YaHei,{fontsize},&H00FFFFFF,&H00FFFFFF,&H00101010,"
        f"&H7F000000,0,0,0,0,100,100,0,0,1,{outline},0,2,60,60,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [
        f"Dialogue: 0,{_ass_ts(e.start_ms)},{_ass_ts(e.end_ms)},Default,,0,0,0,,{_ass_text(e.text)}"
        for e in entries
    ]
    return header + "\n".join(lines) + "\n"


def write_subtitle_files(
    entries: list[SubEntry], base_path: Path, *, width: int = 1280, height: int = 720,
) -> tuple[Path, Path]:
    """落 ASS + SRT 两个文件，返回 (ass_path, srt_path)。"""
    base_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path = base_path.with_suffix(".ass")
    srt_path = base_path.with_suffix(".srt")
    ass_path.write_text(to_ass(entries, width=width, height=height), encoding="utf-8")
    srt_path.write_text(to_srt(entries), encoding="utf-8")
    return ass_path, srt_path


# ---------- 烧录 ----------


def _subtitles_filter(path: Path | str) -> str:
    """subtitles 滤镜的 Windows 路径转义：\\→/、盘符冒号转义、单引号包裹。"""
    p = str(path).replace("\\", "/").replace(":", "\\:").replace("'", "")
    return f"subtitles='{p}'"


def _ffmpeg_exe() -> str | None:
    from ..video.qa import _ffmpeg

    return _ffmpeg()


def burn_subtitles(
    video_path: Path, sub_path: Path, out_path: Path, *, ffmpeg: str | None = None,
) -> Path:
    """把字幕烧进画面：单次视频重编码（h264 crf18），音轨直拷。"""
    ffmpeg = ffmpeg or _ffmpeg_exe()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，无法烧录字幕（可配置 FFMPEG_PATH）")
    cmd = [
        ffmpeg, "-y", "-loglevel", "error", "-i", str(video_path),
        "-vf", _subtitles_filter(sub_path),
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "copy", str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"字幕烧录失败：{(r.stderr or '')[-300:]}")
    return out_path
