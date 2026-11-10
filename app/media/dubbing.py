# =====================================================================
# dubbing.py —— 成片配音（台词 TTS + 时间轴混音）
#
# 解决什么：视频模型没有「文字→语音」通路（只会产出含混假说话），所以
# 台词不进视频提示词（DP 规则9），成片是「哑剧」；本模块把哑剧变成有声片：
#
#   剧本对白 → 按镜头归属（beats 段对齐）→ 逐句 TTS 合成（按角色音色）
#   → ffprobe 实测每句时长排时间轴 → ffmpeg 混音（环境音压低 + 台词叠加）
#
# 台词归属算法（按镜头对齐到秒级）：
#   把场景 beats 压缩成交替的「动作段 / 对话段」序列——导演正是按这个粒度
#   切镜的——再把段序列按比例均分给镜头。对话段里的对白就落在对应镜头的
#   时间区间内。显式 dialogue_ref 优先于段对齐。
#
# 局限（如实说明）：这是「台词在正确时刻出现」的配音，不是视觉级口型同步
# （lip-sync 需要专门的视觉模型，如 Wav2Lip / Latentsync 类）。人物在说话
# 镜头里有口型动作（视频模型按「报告姿态」生成的），配合后期配音观感自然。
# =====================================================================

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain import Script, Shot, assign_dialogue_ranges
from ..llm.tts import TTSClient
from .casting import (  # assign_voices 为旧调用点保留（已修好串角，见 casting.py）
    VOICE_FALLBACK,
    Casting,
    assign_voices,
    cast_script,
)

log = logging.getLogger(__name__)

# 台词排程参数。
LINE_LEAD_SEC = 0.4     # 镜头开始后多久说第一句
LINE_GAP_SEC = 0.25     # 句间停顿


# ---------- 台词归属 ----------


@dataclass
class DubLine:
    """一句待合成的台词。"""

    speaker: str
    text: str
    voice_id: str
    shot_id: str
    at_sec: float | None = None  # 镜头内第几秒开口（导演给的 dialogue_at）；None=镜头起始后默认位置
    start_sec: float = 0.0  # 合成后排程填入（成片内绝对时刻）


@dataclass
class DubbingPlan:
    """整部片的配音计划：台词列表 + 总时长 + 跳过的台词说明。"""

    lines: list[DubLine] = field(default_factory=list)
    total_sec: float = 0.0
    # 只渲染了部分镜头时，落不到任何成片上的台词会被丢弃——必须出声，
    # 否则成片里会出现「没人说话却有字幕」的鬼影（2026-10-31 实测）。
    warnings: list[str] = field(default_factory=list)
    # 选角表：谁配了什么声音、依据是什么。写进成片报告，便于用户核对与推翻。
    cast: Casting | None = None
    # 选角层的不确定（性别没线索 → 暂按男声）。与 warnings 分开：warnings 是
    # 「台词丢了」，这里是「声音可能配错」，两者处置方式不同，不该混在一起。
    cast_warnings: list[str] = field(default_factory=list)


def _scene_beat_map(script: Script) -> dict[str, list]:
    return {sc.id: sc.beats for sc in script.scenes}


def _segment_beats(beats: list) -> list[list]:
    """把 beats 压缩成连续同类型段：[动作段, 对话段, ...]。cue 段忽略。"""
    segments: list[list] = []
    for b in beats or []:
        if b.type == "cue":
            continue
        if segments and segments[-1][0].type == b.type:
            segments[-1].append(b)
        else:
            segments.append([b])
    return segments


def plan_dubbing(
    script: Script,
    shots: list[Shot],
    voices: dict[str, str] | None = None,
    *,
    expected_shots: dict[str, int] | None = None,
    genders: dict[str, str] | None = None,
    appearances: dict[str, str] | None = None,
    visual_style: str | None = None,
    voice_pool: list[dict[str, Any]] | None = None,
) -> DubbingPlan:
    """把剧本对白归属到镜头，产出配音计划。

    - 显式 dialogue_ref 优先：镜头指向哪个 beat，台词就归哪一镜；
    - 否则按「段对齐」：场景 beats 压缩成交替段序列后按比例均分给镜头。

    ``voices`` 是资产库里人工确认过的选角 {角色名: voice_id}；不传则按
    性别线索自动选角（见 app/media/casting.py）——旧的「男女交替」兜底
    会让男主开口是御姐音，已废弃。

    ``expected_shots``：每个场景在版本里的**原始镜头数**。只渲染了其中一部分时
    （常见：用户挑几镜先出片试水），没拍出来的镜头上的台词无处安放——必须丢掉，
    否则会全部堆到已渲染镜头的时间轴上，表现为「两句字幕同时出现 + 整片被提速」。
    这时只保留能通过 dialogue_ref 明确归属的台词，其余记进 warnings。
    """
    cast = cast_script(
        script, casting=voices, genders=genders, appearances=appearances,
        visual_style=visual_style, voice_pool=voice_pool,
    )
    char_map = {c.id: c.name for c in script.characters}
    beat_map = _scene_beat_map(script)
    scene_of = {sc.id: sc for sc in script.scenes}
    ordered = sorted(shots, key=lambda s: s.order)
    plan = DubbingPlan(total_sec=sum(max(1.0, s.duration_sec) for s in ordered), cast=cast)
    plan.cast_warnings.extend(cast.warnings)

    # 每个场景单独对齐（导演按场景拆镜，order 已全局编号但场景内连续）。
    by_scene: dict[str, list[Shot]] = {}
    for s in ordered:
        by_scene.setdefault(s.scene_id, []).append(s)

    for scene_id, scene_shots in by_scene.items():
        beats = beat_map.get(scene_id) or []
        dialogue_beats = [b for b in beats if b.type == "dialogue" and (b.line or "").strip()]
        if not dialogue_beats or not scene_shots:
            continue

        # 该场是否只渲染了一部分镜头
        want = (expected_shots or {}).get(scene_id) or 0
        partial = bool(want and want > len(scene_shots))

        # 1) 按 dialogue_ref 的分界点把台词切到各镜：一镜只拿「从它引用的那句
        #    起、到下一镜引用的那句之前」的台词。一段甲乙来回的对话就能按说话人
        #    拆开，不会把两个人的话都塞进同一镜（观众分不清是谁在说）。
        ranges = assign_dialogue_ranges(scene_shots, beats)
        if ranges:
            assigned = {b.id for bs in ranges.values() for b in bs}
            segments = _segment_beats(beats)
            seg_of = {b.id: si for si, seg in enumerate(segments) for b in seg}
            n_shots = max(1, len(scene_shots))

            dropped = 0
            for b in dialogue_beats:
                if b.id in assigned:
                    continue
                # 只拍了一部分镜头时，没被认领的台词八成属于没拍的那几镜，
                # 不能再硬塞到已渲染的镜头上。
                if partial:
                    dropped += 1
                    continue
                # 全片都拍：按「段对齐」兜底落到对应镜头。
                si = seg_of.get(b.id, 0)
                shot = scene_shots[min(n_shots - 1, si * n_shots // max(1, len(segments)))]
                ranges.setdefault(shot.id, []).append(b)

            for s in scene_shots:
                for b in ranges.get(s.id) or []:
                    speaker = char_map.get(b.speaker or "", b.speaker or "")
                    plan.lines.append(DubLine(
                        speaker=speaker, text=(b.line or "").strip(),
                        voice_id=cast.voice_for(b.speaker or "", speaker),
                        shot_id=s.id, at_sec=s.dialogue_at,
                    ))
            if dropped:
                plan.warnings.append(
                    f"场景 {scene_id} 只渲染了 {len(scene_shots)}/{want} 个镜头，"
                    f"{dropped} 句台词所属镜头未出片，已跳过配音"
                )
            continue

        # 2) 段对齐：镜头 k 对应第 k 个段（导演正是按「动作段/对话段」切镜的）；
        #    段数与镜头数不等时按比例映射。对话段里的全部对白归该镜。
        if partial:
            # 没有 dialogue_ref 可依，段对齐的分配在「部分渲染」下必然错位，整场丢弃。
            plan.warnings.append(
                f"场景 {scene_id} 只渲染了 {len(scene_shots)}/{want} 个镜头，"
                f"且镜头没有 dialogue_ref，本场台词无法对齐，已跳过配音"
            )
            continue
        segments = _segment_beats(beats)
        n = len(scene_shots)
        for si, seg in enumerate(segments):
            if seg[0].type != "dialogue":
                continue
            shot = scene_shots[min(n - 1, si * n // len(segments))]
            for b in seg:
                if not (b.line or "").strip():
                    continue
                speaker = char_map.get(b.speaker or "", b.speaker or "")
                plan.lines.append(DubLine(
                    speaker=speaker, text=(b.line or "").strip(),
                    voice_id=cast.voice_for(b.speaker or "", speaker),
                    shot_id=shot.id, at_sec=shot.dialogue_at,
                ))
    return plan


# ---------- 合成 + 排程 ----------


def _audio_duration_sec(path: Path, ffprobe: str | None) -> float:
    """实测音频时长：优先 ffprobe；便携版常缺 ffprobe，回退用 ffmpeg -i 解析。"""
    if ffprobe:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        try:
            return max(0.5, float(r.stdout.strip()))
        except ValueError:
            pass
    ffmpeg = _ffmpeg_exe()
    if ffmpeg:
        r = subprocess.run(
            [ffmpeg, "-i", str(path)], capture_output=True, text=True, timeout=30,
        )
        import re

        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
        if m:
            h, mi, s = (float(x) for x in m.groups())
            return max(0.5, h * 3600 + mi * 60 + s)
    return 2.0  # 全部探不到时的保守估计


def _find_ffprobe() -> str | None:
    from ..video.qa import _ffprobe

    return _ffprobe()


def _ffmpeg_exe() -> str | None:
    """ffmpeg 查找：FFMPEG_PATH 配置优先，其次 PATH。"""
    from ..video.qa import _ffmpeg

    return _ffmpeg()


# 可重试的错误特征：限流（MiniMax 1002 RPM）、网关抖动、超时。
# 这些跟内容无关，退避几秒重试通常就过了；而「没有音频」这类是厂商侧的
# 真实拒绝，重试没意义。
_RETRYABLE_HINTS = (
    "rate limit", "1002", "429", "too many requests",
    "timeout", "timed out", "connection", "502", "503", "504",
)

# 音色本身不可用（账号没开通某个系统音色 / 参数被拒）：换同性别兜底音色，
# 而不是把这句台词丢掉——丢一句就是成片里一个人突然哑了。
_VOICE_HINTS = ("voice", "音色", "2013", "invalid param")


def _synth_with_retry(
    tts: TTSClient,
    text: str,
    *,
    voice_id: str,
    speed: float,
    emit: Any,
    attempts: int = 3,
) -> bytes:
    """合成一句台词；遇到限流/临时错误按 2^n 退避重试，音色不可用则换音色。

    为什么要重试：一部长片有几十句台词，批量合成必然撞上 TTS 的 RPM 限制。
    不重试的话这些句子会被「跳过」——成片里就会出现有人说话却没声音的段，
    而且默认无人察觉（2026-10-31 实测：3 镜 17 句里丢了 4 句）。
    """
    voices = [voice_id]
    alt = VOICE_FALLBACK.get(voice_id)
    if alt and alt != voice_id:
        voices.append(alt)

    last: Exception | None = None
    for vi, vid in enumerate(voices):
        for i in range(attempts):
            try:
                return tts.synthesize(text, voice_id=vid, speed=speed)
            except Exception as e:  # noqa: BLE001
                last = e
                hint = str(e).lower()
                if vi + 1 < len(voices) and any(h in hint for h in _VOICE_HINTS):
                    emit(f"[配音] 音色 {vid} 不可用，改用 {voices[vi + 1]}：{e}")
                    break  # 跳出重试，换下一个音色
                retryable = any(h in hint for h in _RETRYABLE_HINTS)
                if i == attempts - 1 or not retryable:
                    raise
                wait = 2 ** (i + 1)
                emit(f"[配音] 触发限流/临时错误，{wait}s 后重试（第 {i + 2}/{attempts} 次）：{e}")
                time.sleep(wait)
    raise last if last else RuntimeError("TTS 合成失败")


def synthesize_track(
    tts: TTSClient,
    plan: DubbingPlan,
    shots: list[Shot],
    work_dir: Path,
    *,
    on_progress: Any = None,
) -> list[dict[str, Any]]:
    """逐句合成台词，按镜头起点 + 实测句长排时间轴。

    预算感知：台词常常挤不进所属镜头（导演按戏剧节奏切镜，不知道 TTS 实长）。
    首轮按常速合成，若最后一句的结束时刻超出片尾预算，则整批以 1.2 倍速
    重合成一次再排——宁可语速稍快，不让台词被片尾截掉。

    返回 [{"path": mp3路径, "start_ms": int, "end_ms": int, "text": str,
          "speaker": str}, ...]（按时间排序）。end_ms 与 speaker 供字幕时间轴复用。
    """
    def _emit(msg: str) -> None:
        log.info(msg)
        if on_progress:
            on_progress(msg)

    def _attempt(speed: float) -> list[dict[str, Any]]:
        shot_start: dict[str, float] = {}
        shot_len: dict[str, float] = {}
        t = 0.0
        for s in sorted(shots, key=lambda x: x.order):
            shot_start[s.id] = t
            shot_len[s.id] = max(1.0, s.duration_sec)
            t += max(1.0, s.duration_sec)
        total = t

        ffprobe = _find_ffprobe()
        work_dir.mkdir(parents=True, exist_ok=True)
        track: list[dict[str, Any]] = []
        next_free: dict[str, float] = {}
        for i, line in enumerate(plan.lines):
            mp3 = work_dir / f"line_{i:03d}.mp3"
            try:
                mp3.write_bytes(_synth_with_retry(
                    tts, line.text, voice_id=line.voice_id, speed=speed, emit=_emit,
                ))
            except Exception as e:  # noqa: BLE001
                _emit(f"[配音] 「{line.text[:12]}…」合成失败，跳过：{e}")
                continue
            dur = _audio_duration_sec(mp3, ffprobe)
            base = shot_start.get(line.shot_id, 0.0)
            # 开口时刻：导演给了 dialogue_at 就落在那一秒（让台词跟画面上那个动作同步），
            # 否则退化为镜头起始后 LINE_LEAD_SEC。两者都夹在本镜范围内——
            # 台词跨出本镜就会盖到下一镜的画面上，观众听到的和看到的对不上。
            span = shot_len.get(line.shot_id, 1e9)
            lead = LINE_LEAD_SEC if line.at_sec is None else max(0.0, float(line.at_sec))
            lead = min(lead, max(0.0, span - min(dur, span - 0.2) - 0.1))
            start = max(base + lead, next_free.get(line.shot_id, 0.0) or base + lead)
            next_free[line.shot_id] = start + dur + LINE_GAP_SEC
            track.append({"path": str(mp3), "start_ms": int(start * 1000),
                          "end_ms": int((start + dur) * 1000),
                          "text": line.text, "speaker": line.speaker,
                          "voice_id": line.voice_id})
            from .casting import VOICE_LABELS

            # 音色名：内置表 → 选角带来的发现名 → 原始 id（D-24 不给用户看工程 id）。
            # getattr 兜底：测试可传裸 plan（没有 cast 字段）。
            cast = getattr(plan, "cast", None)
            label = cast.label_for(line.voice_id) if cast else VOICE_LABELS.get(line.voice_id, line.voice_id)
            _emit(f"[配音] {line.speaker}·{label}"
                  f"（{start:.1f}s 起，{dur:.1f}s）：{line.text}")
        track.sort(key=lambda x: x["start_ms"])
        return track, total

    track, total = _attempt(1.0)
    if track and len(track) > 1:
        # 超预算 → 按实测「内容跨度比」自适应提速（上限 1.6，再快就失真了）。
        # 根源在对白密度：上游分镜应给对白镜头排足时长（导演规则有此约束），
        # 配音层提速只是兜底。跨度比 + 5% 安全余量，抵消 TTS speed 参数的抖动。
        first_start = track[0]["start_ms"] / 1000
        span = track[-1]["start_ms"] / 1000 + _audio_duration_sec(Path(track[-1]["path"]), _find_ffprobe()) - first_start
        span_budget = total - 0.2 - first_start
        if span > span_budget and span_budget > 1:
            speed = min(1.6, round(1.05 * span / span_budget, 2))
            _emit(f"[配音] 台词超出片长预算（{first_start + span:.1f}s > {total:.0f}s），按 {speed} 倍速重合成")
            track, _ = _attempt(speed)
    return track


# ---------- 混音 ----------

def mix_dubbing(
    video_path: Path,
    track: list[dict[str, Any]],
    out_path: Path,
    *,
    bg_volume: float = 0.25,
    ffmpeg: str | None = None,
) -> Path:
    """把台词轨混进成片：环境音压低、台词按 start_ms 延迟叠加。

    视频流直拷（-c:v copy），只重编码音频。amix normalize=0 需要 ffmpeg 4.4+，
    不支持时回退为各路预放大补偿。
    """
    ffmpeg = ffmpeg or _ffmpeg_exe()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，无法混音（可配置 FFMPEG_PATH）")

    inputs = ["-i", str(video_path)]
    for item in track:
        inputs += ["-i", item["path"]]

    def build_filter(normalize: bool) -> str:
        parts: list[str] = []
        mix_labels: list[str] = []
        if track:
            parts.append(f"[0:a]volume={bg_volume}[bg]")
            mix_labels.append("[bg]")
        for i, item in enumerate(track, start=1):
            delay = int(item["start_ms"])
            gain = ""
            if not normalize and track:
                # 老版 amix 会把音量除以路数，给台词路预放大补偿。
                gain = f"volume={len(track) + 1},"
            parts.append(f"[{i}:a]{gain}adelay={delay}|{delay}[d{i}]")
            mix_labels.append(f"[d{i}]")
        if not track:
            return None
        norm = ":normalize=0" if normalize else ""
        parts.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first{norm}[out]")
        return ";".join(parts)

    for normalize in (True, False):
        flt = build_filter(normalize)
        if flt is None:
            # 没有成功合成的台词：原样拷贝
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(video_path), "-c", "copy", str(out_path)]
            break
        cmd = (
            [ffmpeg, "-y", "-loglevel", "error", *inputs,
             "-filter_complex", flt, "-map", "0:v", "-map", "[out]",
             "-c:v", "copy", "-c:a", "aac", "-shortest", str(out_path)]
        )
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            break
        log.debug("混音（normalize=%s）失败，尝试回退：%s", normalize, r.stderr[-300:])
    else:
        raise RuntimeError(f"混音失败：{r.stderr[-300:]}")
    return out_path


def dub_video_with_track(
    video_path: Path,
    script: Script,
    shots: list[Shot],
    tts: TTSClient,
    out_path: Path,
    *,
    work_dir: Path | None = None,
    bg_volume: float = 0.25,
    on_progress: Any = None,
    expected_shots: dict[str, int] | None = None,
    collected: list[str] | None = None,
    casting: dict[str, str] | None = None,
    genders: dict[str, str] | None = None,
    appearances: dict[str, str] | None = None,
    visual_style: str | None = None,
    voice_pool: list[dict[str, Any]] | None = None,
) -> tuple[Path | None, list[dict[str, Any]], DubbingPlan]:
    """一站式配音：计划 → 合成 → 混音。没有台词或 TTS 不可用时返回 (None, [], plan)。

    额外返回台词时间轴（start_ms/end_ms/text/speaker），供字幕与语音同步；
    以及配音计划（含选角表，写进成片报告让用户核对「谁配了什么声音」）。
    ``expected_shots`` 见 plan_dubbing：部分渲染时用它判断哪些台词该丢。
    ``collected``：把丢台词这类「不致命但用户该知道」的告警收集进成片报告。
    """
    plan = plan_dubbing(
        script, shots, casting,
        expected_shots=expected_shots, genders=genders, appearances=appearances,
        visual_style=visual_style, voice_pool=voice_pool,
    )
    if plan.warnings:
        for w in plan.warnings:
            log.warning(w)
            if collected is not None:
                collected.append(w)
            if on_progress:
                on_progress(f"[配音] {w}")
    for w in plan.cast_warnings:
        log.warning(w)
        if collected is not None:
            collected.append(w)
        if on_progress:
            on_progress(f"[配音] {w}")
    if not plan.lines:
        log.info("本片没有对白，跳过配音")
        return None, [], plan

    def _emit(msg: str) -> None:
        log.info(msg)
        if on_progress:
            on_progress(msg)

    _emit(f"[配音] 共 {len(plan.lines)} 句台词，总时长 {plan.total_sec:.0f}s")
    if plan.cast and plan.cast.members:
        roster = "；".join(
            f"{m.name}→{m.voice_label}({m.gender})" for m in plan.cast.members
        )
        _emit(f"[配音] 选角：{roster}")
    tmp = Path(tempfile.mkdtemp(prefix="dub_")) if work_dir is None else work_dir
    try:
        track = synthesize_track(tts, plan, shots, tmp, on_progress=on_progress)
        if not track:
            return None, [], plan
        return mix_dubbing(video_path, track, out_path, bg_volume=bg_volume), track, plan
    finally:
        if work_dir is None:
            shutil.rmtree(tmp, ignore_errors=True)


def dub_video(
    video_path: Path,
    script: Script,
    shots: list[Shot],
    tts: TTSClient,
    out_path: Path,
    *,
    work_dir: Path | None = None,
    bg_volume: float = 0.25,
    on_progress: Any = None,
) -> Path | None:
    """一站式配音。需要台词时间轴（字幕同步）时改用 dub_video_with_track。"""
    video, _, _ = dub_video_with_track(
        video_path, script, shots, tts, out_path,
        work_dir=work_dir, bg_volume=bg_volume, on_progress=on_progress,
    )
    return video
