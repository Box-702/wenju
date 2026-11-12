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


def qa_check(video_url: str, vision: Any = None, *, count: int | None = None) -> dict[str, Any]:
    """逐帧质检成片，聚合判定。

    返回 {"verdict": "pass" | "fail" | "skipped", "issues": [...], "frames": n}。
    没有视觉模型 / 抽帧失败 → skipped（质检不阻断任务）。
    """
    from ..config import get_settings

    n = count or max(3, min(9, get_settings().video_qa_frames or 5))
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

    result = qa_check(job.video_url)
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

    new_job = store.create_video_job(
        project_id=job.project_id,
        shot_id=job.shot_id,
        provider=job.provider,
        model=job.model,
        prompt=job.prompt,
        params={**params, "qa_attempt": attempt + 1},
        version_id=job.version_id,
        cost_estimate=job.cost_estimate,
    )
    submit(new_job)
    log.warning("成片质检不合格，自动重 roll（job=%s → %s，第 %d 次）：%s",
                job_id, new_job.id, attempt + 1, issues)
    return new_job.id
