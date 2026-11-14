# =====================================================================
# finalize.py —— 成片合成（拼装 + 配音 + 字幕，产品化接线）
#
# 在此之前，assemble_videos / dub_video 只有测试脚本在用：批次跑完只把状态
# 标成 done，用户拿到的一直是单镜视频。本模块把「最后一公里」接进产品：
#
#   版本快照里已成功的镜头 → 下载各镜 mp4 → 拼装 → 台词配音（可选）
#   → 字幕烧录（可选，时间轴优先用 TTS 实测值）→ 成片 + SRT 落盘为项目素材
#
# 降级阶梯（每步失败都不阻断交付，状态如实回报）：
#   某镜下载失败 → 跳过该镜继续；配音失败/无 TTS → 静片 + 估算字幕时间轴；
#   烧录失败 → 交付无字幕成片。真正致命的只有「一个可用镜头都没有」和「拼装失败」。
#
# 状态在内存（与 BatchManager 同模式，进程重启后进行中的合成作废）；
# 产物本身已落盘并注册为项目素材，不受重启影响。
# =====================================================================

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..domain import Script, Shot
from ..media.dubbing import dub_video_with_track
from ..media.subtitles import burn_subtitles, timeline_estimated, timeline_from_track, write_subtitle_files

log = logging.getLogger(__name__)

# 下载/合成的单镜超时与轮询已由 provider 侧保证，这里只约束下载。
_DOWNLOAD_TIMEOUT = 300


@dataclass
class FinalizeOptions:
    """成片合成选项（用户可选项都收在这里）。"""

    subtitles: bool = True   # 台词字幕烧录
    audio_mode: str = "native"  # native 原生音画同生（默认）/ dub TTS 配音 / silent 静片


def _emit(rec: dict[str, Any], msg: str) -> None:
    log.info("[finalize] %s", msg)
    rec["messages"].append(msg)
    del rec["messages"][:-40]  # 只保留最近的进度，避免长片刷屏


def _latest_succeeded_url(jobs: list, shot_id: str, version_id: str | None = None) -> str | None:
    """该镜头最近一次成功任务的成片 URL（版本快照可能没回填 video_url）。

    按 version_id 隔离（版本隔离收口）：只取「本版本的成功任务」；本版本一条
    都没有时回退到「无版本归属」的历史任务（单镜手提交/对账回写常不带
    version_id，无法证明是别代，与 _auto_references 同一口径放行）。
    **明确属于其他版本的任务绝不混入**——旧版本合成拼进新版本成片，
    内容与镜头方案对不上，且用户从成片里看不出来。
    """
    candidates = [
        j for j in jobs
        if j.shot_id == shot_id and j.status == "succeeded" and j.video_url
    ]
    if version_id:
        same = [j for j in candidates if j.version_id == version_id]
        if same:
            candidates = same
        else:
            candidates = [j for j in candidates if not j.version_id]
    urls = [(j.created_at, j.video_url) for j in candidates]
    return max(urls)[1] if urls else None


def _download(url: str, dest: Path) -> None:
    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True, trust_env=False) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)


def _resolve_script(store: Any, project: Any) -> Script | None:
    """项目的当前剧本（配音与字幕的台词来源）。取不到就降级为无对白。"""
    vid = project.current_version_id
    if not vid:
        return None
    try:
        sv = store.get_version(vid)
        return sv.script if sv else None
    except Exception as e:  # noqa: BLE001
        log.warning("剧本读取失败，成片将无对白/字幕：%s", e)
        return None


def _resolve_tts(store: Any = None):
    from ..config import get_settings
    from ..llm.registry import resolve_tts
    from ..llm.tts import build_tts_client

    cfg = resolve_tts(get_settings(), store)
    return build_tts_client(cfg) if cfg else None


def _register_asset(store: Any, project_id: str, path: Path, *, kind: str, name: str) -> dict[str, Any]:
    """成片/SRT 文件本体存入数据库（media_assets.content），随后删除临时文件。

    合成过程需要本地文件喂 ffmpeg，这里负责「最后一公里」：字节入库、
    临时产物清理，保证 finalize 结束后本地不留成品文件。
    """
    from ..store import gen_id

    asset_id = gen_id("asset")
    data = path.read_bytes()
    path.unlink(missing_ok=True)
    store.add_asset(project_id, kind=kind, name=name, source="finalize",
                    url=f"/api/assets/file/{asset_id}", id=asset_id, content=data)
    return {"asset_id": asset_id, "url": f"/api/assets/file/{asset_id}"}


def finalize_film(
    project_id: str,
    *,
    version_id: str | None = None,
    subtitles: bool = True,
    audio_mode: str = "native",
    on_progress: Any = None,
    store: Any = None,
) -> dict[str, Any]:
    """把已生成的镜头合成为完整成片。阻塞执行（调用方负责放线程里跑）。

    audio_mode（2026-11-12 音画同生定案，D-46/D-47）：
      native —— 默认。保留视频模型的原生音轨（台词/环境声都来自提示词，
              唇形同步是它的原生能力）；TTS 不参与。
      dub    —— 两段式：TTS 配音混入、原生音轨压成环境声底；配音失败
              且本片有台词时剥音轨交静片（旧行为，兜底不乱响）。
      silent —— 一律静片（字幕承担台词）。
    返回 result 字典；致命失败（无可用镜头/拼装失败）抛异常，由调用方记录。
    """
    audio_mode = (audio_mode or "native").strip().lower()
    if audio_mode not in ("native", "dub", "silent"):
        audio_mode = "native"
    store = store or _default_store()
    rec: dict[str, Any] = {"messages": []}

    def emit(msg: str) -> None:
        _emit(rec, msg)
        if on_progress:
            on_progress(msg)

    project = store.get_project(project_id)
    if project is None:
        raise ValueError(f"项目不存在：{project_id}")

    # ---- 1) 收集镜头：版本快照定顺序，成片 URL 取各镜最近一次成功任务 ----
    vid = version_id or project.current_video_version_id
    version = store.get_video_version(vid) if vid else None
    if version is None:
        raise ValueError("没有可合成的视频版本（先生成分镜并出片）")
    shot_dicts = [s for s in version.shots if isinstance(s, dict)]
    if not shot_dicts:
        raise ValueError("该版本没有镜头")
    jobs = store.list_video_jobs(project_id)

    # 每个场景在版本里原本有几镜：配音/字幕据此判断「是否只渲染了一部分」。
    expected_per_scene: dict[str, int] = {}
    for raw in shot_dicts:
        sid = str(raw.get("scene_id") or "")
        if sid:
            expected_per_scene[sid] = expected_per_scene.get(sid, 0) + 1

    shots: list[Shot] = []
    tmp = Path(tempfile.mkdtemp(prefix="finalize_"))
    try:
        clips: list[str] = []
        warnings: list[str] = []
        # ---- 镜头组展开（S2b，PLAN-shot-group）----
        # 生成单元=一次提交：组任务挂在组头 shot_id 上，一条 clip 覆盖全组；
        # 成员镜头不各自找任务（永远找不到，也不该重复下载）。
        from .service import _generation_units

        unit_by_head = {u["shot_id"]: u for u in _generation_units(shot_dicts)}
        ready = 0
        # 每个 clip 覆盖几个成员镜头（校准循环按它把 shots 切段配对 clips）
        unit_member_counts: list[int] = []
        for raw in sorted(shot_dicts, key=lambda s: s.get("order", 0)):
            sid = str(raw.get("id") or "")
            unit = unit_by_head.get(sid)
            if unit is None:
                continue  # 组员：由组头代表下载
            url = _latest_succeeded_url(jobs, sid, vid)
            if not url:
                warnings.append(f"镜头 {sid} 还没有成片，已跳过")
                emit(f"镜头 {sid} 没有成片，跳过")
                continue
            dest = tmp / f"{sid}.mp4"
            try:
                _download(url, dest)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"镜头 {sid} 下载失败：{e}")
                emit(f"镜头 {sid} 下载失败，跳过：{e}")
                continue
            member_ids = unit["member_ids"]
            members_raw = [m for m in shot_dicts if str(m.get("id") or "") in member_ids]
            members_raw.sort(key=lambda m: int(m.get("group_index") or 0))
            # 拼接列表：一条 clip 只进一次（组视频本身就是整段戏，重复拼=播 N 遍）；
            # 时间轴列表（shots）按成员展开——配音/字幕用它排每句台词落在第几秒。
            clips.append(str(dest))
            unit_member_counts.append(len(members_raw))
            for m in members_raw:
                shots.append(Shot.model_validate(m))
            ready += len(members_raw)
        if not clips:
            raise ValueError("没有任何已生成的镜头可合成（先生成视频再合成成片）")
        emit(f"已就绪 {ready}/{len(shot_dicts)} 个镜头")

        # provider 实际产出与计划时长有偏差（v6 实测：计划 20s，成片 21.2s），
        # 按计划时长排配音/字幕会越往后漂移越大——下载后逐镜实测校准。
        # 组 clip 只量一次，成员按**计划时长比例**分摊实测值：配音时间轴
        # 的组内对位由此保持（S2 风险清单第 2 条，专项锁测在 test_finalize_group）。
        calibrated: list[Shot] = []
        idx = 0
        for clip, count in zip(clips, unit_member_counts, strict=True):
            group = shots[idx:idx + count]
            real = _media_duration_sec(Path(clip))
            if real > 0 and count == 1:
                calibrated.append(group[0].model_copy(update={"duration_sec": real}))
            elif real > 0:
                plan_total = sum(max(0.5, s.duration_sec) for s in group)
                for s in group:
                    share = max(0.5, s.duration_sec) / plan_total * real
                    calibrated.append(s.model_copy(update={"duration_sec": round(share, 2)}))
            else:
                calibrated.extend(group)
            idx += count
        if any(a.duration_sec != b.duration_sec for a, b in zip(calibrated, shots, strict=True)):
            emit(f"按实际成片时长校准时间轴（合计 {sum(s.duration_sec for s in calibrated):.1f}s）")
        shots = calibrated

        # ---- 2) 拼装 ----
        from .assemble import assemble_videos

        base = tmp / "base.mp4"
        result = assemble_videos(clips, str(base))
        if not result.get("ok"):
            raise RuntimeError(f"拼装失败：{result.get('error')}")
        emit(f"拼装完成（{result.get('duration', 0):.1f}s）")

        # ---- 3) 声音：按 audio_mode 分流（2026-11-12 音画同生定案）----
        current, track = base, []
        script = _resolve_script(store, project)
        dubbed = False
        cast_rows: list[dict[str, Any]] = []
        if audio_mode == "dub" and script is not None:
            tts = _resolve_tts(store)
            if tts is None or not tts.available:
                emit("未配置 TTS（可在设置页配置语音合成），跳过配音")
            else:
                try:
                    dubbed_path = tmp / "dubbed.mp4"
                    # 选角以项目资产库为准（人工确认过的）+ 美术指导的外貌锚推断。
                    style = store.get_video_style(project_id) or {}
                    # 音色池：勾选启用的 + 内置兜底 + 克隆（与资产库/选角同一份，
                    # 让发现出来的音色在配音进度与成片报告里显示中文名而不是原始 id）。
                    from ..media.casting import build_voice_pool
                    out, track, plan = dub_video_with_track(
                        current, script, shots, tts, dubbed_path,
                        on_progress=emit,
                        expected_shots=expected_per_scene,
                        collected=warnings,
                        casting=style.get("voice_casting") or None,
                        genders=style.get("character_genders") or None,
                        appearances=style.get("character_appearances") or None,
                        visual_style=style.get("visual_style"),
                        voice_pool=build_voice_pool(store, style.get("visual_style")),
                    )
                    if plan.cast:
                        cast_rows = [m.to_dict() for m in plan.cast.members]
                    if out:
                        current, dubbed = dubbed_path, True
                        emit("配音完成")
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"配音失败，成片为静片：{e}")
                    emit(f"配音失败，跳过：{e}")
        elif audio_mode == "dub":
            emit("剧本不可读或无版本，跳过配音")
        elif audio_mode == "native":
            emit("声音走视频模型原生音轨（台词/环境声随提示词同生成，唇形同步）")

        # ---- 3b) 音轨处理：silent 一律静片；dub 失败时剥假语音；native 保留 ----
        def _strip_audio(tag: str) -> None:
            nonlocal current
            try:
                from .assemble import find_ffmpeg as _find_ff

                ff = _find_ff()
                if ff:
                    import subprocess

                    silent = tmp / "silent.mp4"
                    r = subprocess.run(
                        [ff, "-y", "-loglevel", "error", "-i", str(current),
                         "-c:v", "copy", "-an", str(silent)],
                        capture_output=True, text=True, timeout=300)
                    if r.returncode == 0 and silent.exists():
                        current = silent
                        emit(tag)
            except Exception as e:  # noqa: BLE001
                log.debug("剥音轨失败（保留原音轨交付）：%s", e)

        if audio_mode == "silent":
            _strip_audio("静片模式：已移除全部音轨（台词由字幕承担）")
        elif audio_mode == "dub" and not dubbed and script is not None:
            # dub 模式下模型原生音轨是含混"叽喳声"（台词没进提示词的旧产物），
            # 配音又没成功——静片 + 字幕远好于假语音（2026-11-12 v2 反馈）。
            has_dialogue = any(
                (b.type == "dialogue" and (b.line or "").strip())
                for sc in (script.scenes if script else []) for b in sc.beats
            )
            if has_dialogue:
                _strip_audio("配音未产出台词音轨：已剥除模型自生语音，交付静片+字幕")

        # ---- 4) 字幕（可选）----
        subtitled = False
        srt_info: dict[str, Any] | None = None
        entries: list = []
        if subtitles:
            if script is not None:
                entries = (
                    timeline_from_track(track)
                    if track
                    else timeline_estimated(script, shots, expected_shots=expected_per_scene)
                )
            if not entries:
                # 无剧本版本（import_storyboard 直导的分镜作品）或剧本没有
                # 对白节拍：台词兜底从镜头提示词提取（官方句式/花括号）。
                # 旧行为是直接放弃字幕——果园 2026-11-14 用户实测「没字幕」。
                from ..media.subtitles import timeline_from_prompts

                entries = timeline_from_prompts(shots)
                if entries:
                    emit(f"字幕走提示词兜底（{len(entries)} 句）")
        if entries:
            try:
                # 字幕画布按成片实测尺寸：字号/边距由 to_ass 从 height 推导，
                # 写死 1280x720 时竖屏 9:16 成片会按横屏画布缩放、字变小
                # （2026-11-12 画幅默认改横屏，两种比例都会出现）。
                from .assemble import get_video_info

                _vi = get_video_info(str(current))
                _w = int(_vi.get("width") or 0) or 1280
                _h = int(_vi.get("height") or 0) or 720
                ass_path, _srt = write_subtitle_files(entries, tmp / "film", width=_w, height=_h)
                final = tmp / "final.mp4"
                burn_subtitles(current, ass_path, final)
                current, subtitled = final, True
                emit(f"字幕烧录完成（{len(entries)} 句）")
                srt_info = _register_asset(store, project_id, _srt, kind="subtitle", name="成片字幕.srt")
            except Exception as e:  # noqa: BLE001
                warnings.append(f"字幕烧录失败，交付无字幕成片：{e}")
                emit(f"字幕烧录失败：{e}")
        elif subtitles:
            emit("本片没有台词，无需字幕")

        # ---- 5) 交付：成片注册为项目素材 ----
        # 时长要在 move 进素材目录之前量（move 后原路径不存在）。
        duration = _media_duration_sec(current)
        if duration <= 0:
            duration = sum(max(1.0, s.duration_sec) for s in shots)  # 兜底：校准后的计划时长
        video_info = _register_asset(store, project_id, current, kind="video",
                                     name=f"成片·{project.title}.mp4")
        emit("成片已交付")
        return {
            "video_asset_id": video_info["asset_id"],
            "video_url": video_info["url"],
            "srt_asset_id": srt_info["asset_id"] if srt_info else None,
            "srt_url": srt_info["url"] if srt_info else None,
            "duration_sec": round(duration, 2),
            "shots_used": len(shots),   # 成员镜头数（组展开后），不是生成单元数
            "shots_total": len(shot_dicts),
            "dubbed": dubbed,
            "subtitled": subtitled,
            "cast": cast_rows,
            "warnings": warnings,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _media_duration_sec(path: Path) -> float:
    """实测媒体时长：优先 ffprobe；缺 ffprobe 回退解析 `ffmpeg -i` 的 stderr。

    全部探不到返回 0.0，调用方回退到计划时长（与配音模块的兜底思路一致，
    本机便携版 ffmpeg 常配不到 ffprobe）。
    """
    import re
    import subprocess

    from .qa import _ffmpeg, _ffprobe

    try:
        probe = _ffprobe()
        if probe:
            r = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            return float(r.stdout.strip())
        ffmpeg = _ffmpeg()
        if ffmpeg:
            r = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True, timeout=30)
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
            if m:
                h, mi, s = (float(x) for x in m.groups())
                return h * 3600 + mi * 60 + s
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _default_store():
    from ..deps import store as _store

    return _store()


# ---- 状态注册表（内存，与 BatchManager 同模式） ----


class FinalizeManager:
    """合成任务的状态注册与后台线程执行。"""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self, project_id: str, *, version_id: str | None = None,
              subtitles: bool = True, audio_mode: str = "native",
              dubbing: bool | None = None) -> dict[str, Any]:
        # 兼容旧调用：dubbing=True/False（未给 audio_mode 时）映射 dub/native。
        if audio_mode == "native" and dubbing is True:
            audio_mode = "dub"
        fid = f"fin_{int(time.time() * 1000):x}"
        rec: dict[str, Any] = {
            "id": fid, "project_id": project_id, "status": "running",
            "messages": [], "result": None, "error": None,
            "created_at": time.time(),
        }
        with self._lock:
            self._jobs[fid] = rec
        t = threading.Thread(target=self._run, args=(rec, version_id, subtitles, audio_mode),
                             daemon=True, name=f"finalize-{fid}")
        t.start()
        return dict(rec)

    def _run(self, rec: dict[str, Any], version_id: str | None,
             subtitles: bool, audio_mode: str) -> None:
        try:
            result = finalize_film(
                rec["project_id"], version_id=version_id, subtitles=subtitles,
                audio_mode=audio_mode, on_progress=lambda m: _emit(rec, m),
            )
            rec["result"] = result
            rec["status"] = "succeeded"
        except Exception as e:  # noqa: BLE001
            log.exception("成片合成失败")
            rec["error"] = str(e)
            rec["status"] = "failed"

    def get(self, finalize_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._jobs.get(finalize_id)
            return dict(rec) if rec else None

    def latest_for_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = [r for r in self._jobs.values() if r["project_id"] == project_id]
            return dict(max(found, key=lambda r: r["created_at"])) if found else None


_manager: FinalizeManager | None = None


def get_finalize_manager() -> FinalizeManager:
    global _manager
    if _manager is None:
        _manager = FinalizeManager()
    return _manager
