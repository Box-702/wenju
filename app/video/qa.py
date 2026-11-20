# =====================================================================
# qa.py —— 成片自动质检 + 有界重 roll
#
# 背景：视频模型是「统计画家」而不是「世界模拟器」——单次生成是抽签，
# 穿模 / 世界崩坏 / 严重畸变无法归零。本模块把「抽签」变「有界良率」：
#   成功回调 → 下载成片 → ffmpeg 等距抽帧 → 视觉模型逐帧判定
#   → 致命问题才 FAIL → 有重 roll 预算就用同参数自动重新提交。
#
# 设计约束：
#   - 判定标准只对「致命问题」FAIL（水印/色调/轻微模糊不算），沿用定妆图
#     质检的教训：把小瑕疵也算 FAIL 会导致永远不合格、重试白跑；
#   - 重 roll 次数记在 params JSON 里（qa_attempt），不改数据库表；
#   - 质检结果写回 params["qa"]，失败降级为 skipped，绝不阻断任务状态。
# =====================================================================

from __future__ import annotations

import base64
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .assemble import find_ffmpeg, find_ffprobe

log = logging.getLogger(__name__)

# 逐帧判定问题（只对致命问题 FAIL）。
QA_QUESTION = (
    "你在给 AI 短剧成片做质检。这是同一条视频里按时间等距抽出的一帧。\n"
    "只有以下三类**致命**问题判 FAIL（任一出现即 FAIL）：\n"
    "A) 明显穿模：肢体、道具、人物互相穿透或穿过身体"
    "（接触动作里看不出遮挡关系的暧昧瞬间不算）；\n"
    "B) 世界崩坏：空间结构不成立、主体凭空消失、凭空多出重复的人；\n"
    "C) 画面严重畸变：人脸或肢体扭曲到无法辨认。\n"
    "注意：平台水印、色调偏差、构图瑕疵、轻微模糊、背景路人的细节错误都**不算** FAIL。\n"
    "最后两行严格按格式输出：\n"
    "VERDICT: PASS 或 FAIL\n"
    "ISSUE: 一句话说明（PASS 时写 none）"
)

_VERDICT_FAIL = re.compile(r"VERDICT\s*[:：]\s*FAIL", re.IGNORECASE)
_VERDICT_PASS = re.compile(r"VERDICT\s*[:：]\s*PASS", re.IGNORECASE)
_ISSUE_LINE = re.compile(r"^\s*ISSUE\s*[:：]\s*(.+)$", re.MULTILINE | re.IGNORECASE)

# ---- 声音闸门（2026-11-20《替身v2》复盘 A4）----
#
# 视觉质检看不见声音：native 音画同生下台词镜约一半根本没出人声
# （14 个成片实测：4 个台词镜只有 2 个语音频段有能量；006_024 字幕写着
# 「辛苦了，喝点水吧」而音轨 mean −33.4dB = 纯环境噪声），一路漏到成片。
#
# 阈值按同一批成片校准（不是拍的）：语音频段（200–3500Hz 带通）
# mean_volume > −32dB 算有人声。分界干净——有人声的台词镜 −19～−30dB，
# 没人声的台词镜 −35.9dB 正落在无台词镜的噪声区（−33～−44dB）。
VOICE_MIN_DB = -32.0
_SPEECH_BAND_FILTER = "highpass=f=200,lowpass=f=3500,volumedetect"
# 官方音画台词句式（与 dp._OFFICIAL_DLG_RE 同判据）：冒号挂引号才算台词。
# 招牌引用（写下「…」）不带冒号，不该被要求出人声。
_PROMPT_DIALOGUE_RE = re.compile(r"[:：]\s*[「『“\"]")


def prompt_has_dialogue(prompt: str) -> bool:
    """这条提示词（单镜或组提示词）里有没有该出人声的台词。"""
    return bool(_PROMPT_DIALOGUE_RE.search(prompt or ""))


def _download_clip(video_url: str, dest: Path) -> bool:
    try:
        with httpx.Client(timeout=120, follow_redirects=True,
                          trust_env=False) as client:
            with client.stream("GET", video_url) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("声音检测下载失败（按跳过处理）：%s", e)
        return False


def _speech_band_mean_db(path: Path) -> float | None:
    """语音频段平均音量（dB）。ffmpeg 缺失或测量失败 → None（按跳过处理）。"""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return None
    import subprocess

    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path),
             "-af", _SPEECH_BAND_FILTER, "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("声音检测失败（按跳过处理）：%s", e)
        return None
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else None


def _has_audio_stream(path: Path) -> bool | None:
    """成片有没有音频流：True 有 / False 没有 / None 探测不可用。"""
    probe = _ffprobe()
    if not probe:
        # 便携版常缺 ffprobe：回退解析 ffmpeg -i 的 Stream # 行。
        ffmpeg = _ffmpeg()
        if not ffmpeg:
            return None
        import subprocess

        try:
            r = subprocess.run([ffmpeg, "-i", str(path)],
                               capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
        return "Audio:" in (r.stderr or "")
    import subprocess

    try:
        r = subprocess.run(
            [probe, "-v", "quiet", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    return "audio" in (r.stdout or "")


def voice_check(video_url: str) -> dict[str, Any]:
    """台词镜的声音存在性检测（确定性，零 LLM 零花费）。

    返回 {"verdict": "pass"|"fail"|"skipped", "mean_db": float|None, "note": str}。
    video_url 可为本地路径（离线/测试用）。ffmpeg 缺失 → skipped
    （与视觉质检同口径：工具不齐不阻断任务）。
    """
    if not _ffmpeg():
        return {"verdict": "skipped", "mean_db": None, "note": "找不到 ffmpeg"}
    try:
        with tempfile.TemporaryDirectory(prefix="voice_qa_") as tmp:
            local = Path(video_url)
            if local.exists():
                clip = local
            else:
                clip = Path(tmp) / "clip.mp4"
                if not _download_clip(video_url, clip):
                    return {"verdict": "skipped", "mean_db": None, "note": "下载失败"}
            mean = _speech_band_mean_db(clip)
            if mean is None:
                # 读不出 mean：要么没有音轨，要么测量异常。
                # 没有音轨的台词镜必然「有字幕没声音」→ fail；异常保守按 skipped
                #（误重 roll 花钱，误放过有人工复核）。
                if _has_audio_stream(clip) is False:
                    return {"verdict": "fail", "mean_db": None, "note": "成片没有音轨"}
                return {"verdict": "skipped", "mean_db": None, "note": "音量测量异常"}
    except Exception as e:  # noqa: BLE001
        log.warning("声音检测异常（按跳过处理）：%s", e)
        return {"verdict": "skipped", "mean_db": None, "note": str(e)[:80]}
    if mean < VOICE_MIN_DB:
        return {"verdict": "fail", "mean_db": mean,
                "note": f"语音频段平均 {mean:.1f}dB（≤{VOICE_MIN_DB}dB 噪声底），台词没出人声"}
    return {"verdict": "pass", "mean_db": mean, "note": ""}


def _ffmpeg() -> str | None:
    from ..config import get_settings

    configured = (get_settings().ffmpeg_path or "").strip()
    # 配置路径可能因目录搬迁/删除失效，失效时回退自动探测，避免静默拿到死路径
    if configured and Path(configured).exists():
        return configured
    return find_ffmpeg()


def _ffprobe() -> str | None:
    from ..config import get_settings

    configured = (get_settings().ffmpeg_path or "").strip()
    if configured:
        probe = Path(configured).with_name("ffprobe.exe" if Path(configured).suffix == ".exe" else "ffprobe")
        if probe.exists():
            return str(probe)
    return find_ffprobe()


def _duration(path: Path) -> float:
    probe = _ffprobe()
    if probe:
        try:
            r = __import__("subprocess").run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            return max(1.0, float(r.stdout.strip()))
        except Exception:  # noqa: BLE001
            pass
    # 便携版常缺 ffprobe：回退解析 `ffmpeg -i` 的 stderr Duration 行（与配音模块同思路），
    # 否则探不到时长的片子只能按 8s 估算抽帧点，长片质检会挤在开头。
    import re
    import subprocess

    ffmpeg = _ffmpeg()
    if ffmpeg:
        try:
            r = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True, timeout=30)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
            if m:
                h, mi, s = (float(x) for x in m.groups())
                return max(1.0, h * 3600 + mi * 60 + s)
        except Exception:  # noqa: BLE001
            pass
    return 8.0  # 全部探不到时的最后估算


def extract_frames(video_url: str, *, count: int = 5) -> list[str]:
    """下载（或读取本地）成片 → ffmpeg 等距抽帧 → base64 data URI 列表。

    video_url 也可以是本地文件路径（离线质检/测试用）。
    data URI 由服务端直接解码，比外链 URL 稳（URL 可能过期/需要鉴权）。
    任一环节失败返回空列表，调用方按「跳过质检」处理。
    """
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        log.info("成片质检跳过：找不到 ffmpeg（可配置 FFMPEG_PATH）")
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="video_qa_") as tmp:
            local = Path(video_url)
            if local.exists():
                raw = local
            else:
                raw = Path(tmp) / "clip.mp4"
                with httpx.Client(timeout=120, follow_redirects=True, trust_env=False) as client:
                    with client.stream("GET", video_url) as resp:
                        resp.raise_for_status()
                        with raw.open("wb") as f:
                            for chunk in resp.iter_bytes():
                                f.write(chunk)
            total = _duration(raw)
            import subprocess

            frames: list[str] = []
            for i in range(1, max(1, count) + 1):
                t = total * i / (max(1, count) + 1)
                out = Path(tmp) / f"f{i}.jpg"
                r = subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
                     "-i", str(raw), "-frames:v", "1", "-q:v", "3", str(out)],
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode == 0 and out.exists():
                    b64 = base64.b64encode(out.read_bytes()).decode()
                    frames.append(f"data:image/jpeg;base64,{b64}")
            return frames
    except Exception as e:  # noqa: BLE001
        log.warning("成片抽帧失败，质检跳过：%s", e)
        return []


def _parse_answer(text: str) -> tuple[bool, str]:
    """解析单帧判定：致命问题才 FAIL；解析不到时保守按通过（避免白跑重试）。"""
    if _VERDICT_FAIL.search(text):
        issue = _ISSUE_LINE.search(text)
        return False, (issue.group(1).strip() if issue else text.strip()[-120:])
    if _VERDICT_PASS.search(text) or "PASS" in text.upper():
        return True, "none"
    return True, "（未能解析判定，按通过处理）"


def frame_count_for(duration_sec: float | None) -> int:
    """抽帧数按片长放大。

    固定 5 帧对 4 秒的单镜够用，对 15 秒的镜头组就太稀——组里几个机位挤在
    15 秒里，抽 5 帧很可能整个机位都没抽到，缺陷就这么漏过去了。
    经验值：每 2 秒一帧，下限取配置值（VIDEO_QA_FRAMES），上限 9 帧。
    """
    from ..config import get_settings

    base = max(3, min(9, get_settings().video_qa_frames or 5))
    if not duration_sec or duration_sec <= 0:
        return base
    scaled = int(round(float(duration_sec) / 2.0))
    return max(base, min(9, scaled))


def qa_check(
    video_url: str,
    vision: Any = None,
    *,
    count: int | None = None,
    duration_sec: float | None = None,
) -> dict[str, Any]:
    """逐帧质检成片，聚合判定。

    返回 {"verdict": "pass" | "fail" | "skipped", "issues": [...], "frames": n}。
    没有视觉模型 / 抽帧失败 → skipped（质检不阻断任务）。
    duration_sec 给了就按片长放大抽帧数（组比单镜长，抽得稀会漏检）。
    """
    from ..config import get_settings

    n = count or frame_count_for(duration_sec)
    frames = extract_frames(video_url, count=n)
    if not frames:
        return {"verdict": "skipped", "issues": [], "frames": 0}
    if vision is None:
        try:
            from ..deps import llm as get_llm

            facade = get_llm()
            vision = facade.vision() if facade else None
        except Exception:  # noqa: BLE001
            vision = None
        if vision is None or not vision.available:
            # deps 侧解析失败（如 DB 不可用）时，回退到纯 .env 解析。
            try:
                from ..llm.registry import resolve_vision
                from ..llm.vision import VisionClient

                cfg = resolve_vision(get_settings())
                vision = VisionClient(cfg) if cfg else None
            except Exception:  # noqa: BLE001
                vision = None
    if vision is None or not vision.available:
        return {"verdict": "skipped", "issues": [], "frames": 0}

    issues: list[str] = []
    for idx, frame in enumerate(frames, 1):
        try:
            ok, issue = _parse_answer(vision.ask(frame, QA_QUESTION))
        except Exception as e:  # noqa: BLE001
            log.warning("成片质检第 %d 帧调用失败，按通过处理：%s", idx, e)
            continue
        if not ok:
            issues.append(f"第{idx}帧：{issue}")
    return {
        "verdict": "fail" if issues else "pass",
        "issues": issues,
        "frames": len(frames),
    }


# ---------- 反馈驱动降级重 roll ----------
# 同参数重 roll 只治随机性，治不了系统性：孔乙己实测（2026-11-12）FAIL 集中在
# 手指畸变/穿模/多人同框，这类构图重抽一万次还是同一个分布。重 roll 前把
# 质检结论翻译成「构图降级改写」，命中率才真正上去。规则与剧组提示词 5b 同源。

REWRITE_SYSTEM = (
    "你是短剧视频提示词的缺陷修复员。上一版提示词生成的视频被质检判为致命缺陷，"
    "请改写提示词绕开缺陷。改写规则：\n"
    "- 手指/手部畸变：指尖级动作改成中景结果态（「铜钱已摊在柜台，他的手正从上面收回去」），"
    "删掉对手指分节的逐步描写；\n"
    "- 穿模/肢体融合：全镜只保留一次主接触，写明遮挡与让位关系；其余人物改为静止或大姿态"
    "（倚墙、抱臂听、盯着自己碗里）；\n"
    "- 面部扭曲：镜头拉远到肩胸以上，面部特写里手不入画；\n"
    "- 多人同框崩坏/凭空多出的人：一镜最多 2 人做精细动作，其余人静止；\n"
    "- 场景锚、光线方向、运镜（保持唯一一种主要运动）与结尾画面必须原样保留；\n"
    "- 只写正向描述，禁止否定句。\n"
    "只输出改写后的提示词本身：不要解释、不要引号或代码围栏、不要缩短原文里与缺陷无关的部分。"
)

# 确定性兜底：质检关键词 → 追加的降级指令（LLM 不可用/改写失败时用）。
_RETRY_FIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("手指", "指节", "多指", "手部", "手穿", "手与", "手掌"),
     "本镜改为中景构图：指尖级动作只保留结果状态（道具已放定，手正收回），不逐步描写手指。"),
    (("穿模", "穿透", "融合", "粘连", "融接", "嵌入"),
     "人物与道具只保留一次接触，写明遮挡与让位关系；其余人物改为静止大姿态。"),
    (("面部", "脸", "眼球", "五官"),
     "镜头拉远到肩胸以上，面部特写里手不入画。"),
    (("多出", "重复的人", "虚影", "凭空", "重叠"),
     "画面里最多两人做精细动作，其余人物各自静止或保持单一大幅姿态。"),
)


def _fallback_rewrite(prompt: str, issues: list[str]) -> str:
    """按质检关键词追加确定性降级指令。"""
    text = "；".join(issues)
    fixes = [fix for keys, fix in _RETRY_FIXES if any(k in text for k in keys)]
    if not fixes:
        return prompt
    return prompt.rstrip() + "\n" + "".join(fixes)


def _llm_rewrite(prompt: str, issues: list[str]) -> str | None:
    """拿对话模型按缺陷改写提示词；不可用/结果可疑返回 None。"""
    try:
        from ..deps import llm as get_llm

        facade = get_llm()
        if not facade.available:
            return None
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = facade.chat().invoke([
            SystemMessage(content=REWRITE_SYSTEM),
            HumanMessage(content=(
                f"原提示词：\n{prompt}\n\n质检判定的致命缺陷：\n"
                + "\n".join(f"- {i}" for i in issues[:4])
                + "\n\n输出改写后的提示词："
            )),
        ])
        out = str(resp.content or "").strip()
        # 防抖动：改写结果太短（模型只回了「好的」）说明没干活，按失败处理
        if out and len(out) >= max(20, len(prompt) * 0.3):
            return out
    except Exception as e:  # noqa: BLE001
        log.warning("提示词 LLM 降级改写失败，用确定性兜底：%s", e)
    return None


# 组提示词里的机位标记（DP 折叠时写的：[Shot 1] 0-4s，…）。
_SEG_MARK = re.compile(r"(\[Shot\s+\d+\])")


def _prompt_segments(prompt: str) -> tuple[str, list[str], list[str]]:
    """把组提示词切成 (前缀, 各段标记, 各段正文)。非组提示词返回空。"""
    parts = _SEG_MARK.split(prompt)
    if len(parts) < 5:
        return "", [], []
    return parts[0], parts[1::2], parts[2::2]


def _group_segments(job: Any, store: Any) -> list[tuple[str, float, float]]:
    """组内各机位的时间区间 [(机位 id, 起秒, 止秒)]。

    按 group_member_ids 与各机位的 duration_sec 累加——与 DP 折叠组提示词
    时用的时间轴同口径，所以「第 N 帧」能对回具体哪一机位。
    """
    params = dict(getattr(job, "params", None) or {})
    member_ids = list(params.get("group_member_ids") or [])
    if len(member_ids) < 2 or not getattr(job, "version_id", None):
        return []
    version = store.get_video_version(job.version_id)
    if version is None:
        return []
    by_id = {
        s.get("id"): s for s in (version.shots or [])
        if isinstance(s, dict) and s.get("id")
    }
    out: list[tuple[str, float, float]] = []
    t = 0.0
    for sid in member_ids:
        s = by_id.get(sid)
        if s is None:
            continue
        d = max(0.0, float(s.get("duration_sec") or 0))
        out.append((str(sid), t, t + d))
        t += d
    return out if len(out) >= 2 else []


def _locate_bad_segments(
    issues: list[str],
    segments: list[tuple[str, float, float]],
    duration_sec: float | None,
    frame_count: int | None,
) -> dict[int, list[str]]:
    """把「第 N 帧：…」的缺陷定位到组内第几段机位。返回 {段序号: 该段的缺陷}。"""
    if not segments or not duration_sec or not frame_count:
        return {}
    out: dict[int, list[str]] = {}
    for issue in issues:
        m = re.match(r"第(\d+)帧", str(issue).strip())
        if not m:
            continue
        idx = int(m.group(1))
        # 抽帧是均匀取的，第 idx 帧落在 (idx-0.5)/帧数 * 时长 处
        t = (idx - 0.5) / frame_count * float(duration_sec)
        for si, (_sid, start, end) in enumerate(segments):
            if start <= t < end:
                out.setdefault(si, []).append(issue)
                break
    return out


def _llm_rewrite_segment(prompt: str, body: str, issues: list[str]) -> str | None:
    """只改写一段：把整段提示词当上下文给模型，但只要它输出这一段的改写。

    这样既保住模型的改写能力（它看得见锚与风格段），又保证「只动坏的那一段」
    ——整篇重写会把没坏的机位也搭进去，白降级 = 白花钱且下一版更差。
    """
    try:
        from ..deps import llm as get_llm

        facade = get_llm()
        if not facade.available:
            return None
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = facade.chat().invoke([
            SystemMessage(content=REWRITE_SYSTEM),
            HumanMessage(content=(
                f"整段提示词（上下文，只读）：\n{prompt}\n\n"
                f"要改写的这一段：\n{body}\n\n"
                f"这一段里的致命缺陷：\n"
                + "\n".join(f"- {i}" for i in issues[:4])
                + "\n\n只输出改写后的这一段（不要解释、不要引号）："
            )),
        ])
        out = str(resp.content or "").strip()
        if out and len(out) >= max(10, len(body) * 0.3):
            return out
    except Exception as e:  # noqa: BLE001
        log.warning("提示词分段 LLM 降级改写失败，用确定性兜底：%s", e)
    return None


def _rewrite_group_segments(
    prompt: str,
    issues: list[str],
    bad: dict[int, list[str]],
) -> str:
    """只改写缺陷所在的那几段，其余机位逐字保留。"""
    prefix, marks, bodies = _prompt_segments(prompt)
    if not bodies:
        return prompt
    out = prefix
    for i, (mark, body) in enumerate(zip(marks, bodies, strict=True)):
        if i in bad:
            seg_issues = bad[i]
            body = _llm_rewrite_segment(prompt, body, seg_issues) or _fallback_rewrite(
                body, seg_issues,
            )
        out += mark + body
    return out


def rewrite_prompt_for_retry(
    prompt: str,
    issues: list[str],
    *,
    segments: list[tuple[str, float, float]] | None = None,
    duration_sec: float | None = None,
    frame_count: int | None = None,
) -> str:
    """重 roll 前按质检结论改写提示词；LLM 失败退回确定性追加。

    镜头组：只改写缺陷落在的那一段机位，其余逐字保留。
    """
    if not issues:
        return prompt
    if segments and len(segments) >= 2:
        bad = _locate_bad_segments(issues, segments, duration_sec, frame_count)
        if bad:
            return _rewrite_group_segments(prompt, issues, bad)
    return _llm_rewrite(prompt, issues) or _fallback_rewrite(prompt, issues)


def reroll_if_needed(
    job_id: str,
    job_info: dict[str, Any],
    *,
    store: Any,
    submit: Any,
    max_reroll: int,
) -> str | None:
    """成功回调后的质检重 roll 入口。返回新任务的 job_id（触发了重 roll）或 None。

    - 只处理 succeeded 且带 video_url 的任务；
    - 重 roll 次数记在 params["qa_attempt"]（免改表），有界（max_reroll）；
    - 质检结果写回 params["qa"]；预算用尽仍不合格则保留 succeeded，
      把问题写进 error_message 备注供人复核。
    """
    if str(job_info.get("status")) != "succeeded":
        return None
    job = store.get_video_job(job_id)
    if job is None or not job.video_url:
        return None

    params = dict(job.params or {})
    attempt = int(params.get("qa_attempt") or 0)
    if attempt > max_reroll:  # 安全阀：绝不无限重 roll
        return None
    # 幂等保险：这个任务已经质检过（params 里有 qa 结论）就不再处理。
    # 队列回调若因任何原因重复触发，这里兜住——重 roll 一次就是一次的钱
    # （2026-11-12 孔乙己实测：同一任务被重复回调重 roll 两次，余额烧穿）。
    if params.get("qa"):
        return None

    duration = float(params.get("duration_sec") or 0)
    segments = _group_segments(job, store)

    # 声音闸门（A4，2026-11-20）：台词镜先查「人声到底出没出」——视觉抽帧
    # 看不见声音，006_024 那种「有字幕没声音」的漏过整条质检链路。
    # 确定性测量（ffmpeg 带通 + volumedetect），零 LLM 零花费。
    voice = voice_check(job.video_url) if prompt_has_dialogue(job.prompt) else None

    result = qa_check(job.video_url, duration_sec=duration)
    if voice is not None:
        result = dict(result)
        result["voice"] = voice
        if voice["verdict"] == "fail":
            result["verdict"] = "fail"
            result["issues"] = [f"声音：{voice['note']}"] + list(result.get("issues") or [])
    params["qa"] = result
    store.update_video_job(job_id, params=params)

    if result["verdict"] != "fail":
        if result["verdict"] == "pass":
            log.info("成片质检通过（job=%s，%d 帧抽检）", job_id, result["frames"])
        return None

    issues = "；".join(result["issues"][:2])
    if attempt >= max_reroll:
        store.update_video_job(
            job_id,
            error_message=f"QA 未通过（已重 roll {attempt} 次，人工复核）：{issues}",
        )
        log.warning("成片质检不合格且预算用尽（job=%s）：%s", job_id, issues)
        return None

    # 反馈驱动：重 roll 不是原样重抽，而是按缺陷改写提示词后再抽。
    # 例外——**纯声音缺陷**（画面合格、只是台词没出人声）改写没有着力点：
    # 台词文字本来就在提示词里，音画同生的出声是随机事件，同参数重抽即可
    # （孔乙己实测同参数重 roll 治的就是这类随机性；构图降级规则帮不上）。
    visual_issues = [i for i in (result.get("issues") or []) if not i.startswith("声音：")]
    if voice is not None and voice["verdict"] == "fail" and not visual_issues:
        new_prompt = job.prompt
    else:
        new_prompt = rewrite_prompt_for_retry(
            job.prompt, result["issues"],
            segments=segments, duration_sec=duration, frame_count=result.get("frames"),
        )
    rewritten = new_prompt != job.prompt
    new_job = store.create_video_job(
        project_id=job.project_id,
        shot_id=job.shot_id,
        provider=job.provider,
        model=job.model,
        prompt=new_prompt,
        params={**params, "qa_attempt": attempt + 1,
                **({"qa_rewritten": True} if rewritten else {})},
        version_id=job.version_id,
        cost_estimate=job.cost_estimate,
    )
    submit(new_job)
    log.warning("成片质检不合格，自动重 roll（job=%s → %s，第 %d 次，%s）：%s",
                job_id, new_job.id, attempt + 1,
                "提示词已降级改写" if rewritten else "同参数重抽", issues)
    return new_job.id
