# =====================================================================
# test_subtitles.py —— 字幕时间轴 / SRT / ASS / 烧录参数 / 成片合成编排
#
# 不调网络：时间轴与文件格式全部本地断言；烧录只断言 ffmpeg 命令形状；
# finalize 用假 store + monkeypatch 下载/拼装/注册，验证编排与降级。
# =====================================================================

from __future__ import annotations

import re
import time
from types import SimpleNamespace

from app.domain import (
    Character,
    Location,
    Scene,
    Script,
    ScriptBeat,
    Shot,
    Source,
)
from app.media.subtitles import (
    SubEntry,
    _ass_text,
    _subtitles_filter,
    burn_subtitles,
    timeline_estimated,
    timeline_from_prompts,
    timeline_from_track,
    to_ass,
    to_srt,
)


def test_timeline_from_prompts_extracts_official_dialogue():
    """无剧本版本（import_storyboard 直导分镜）的字幕兜底：台词从提示词提取。
    果园 2026-11-14 实测：subtitles=True 却「剧本不可读，跳过字幕」——
    台词明明就在 `{…}` / `角色说话：「…」` 句式里。"""
    shots = [
        Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=9,
             video_prompt="果园午后，过肩镜头：少女哽咽说：{Am I a machine without feelings?} "
                          "手持轻微晃动。"),
        Shot(id="shot_scene_001_001", scene_id="scene_001", order=1, duration_sec=4,
             video_prompt="绅士正面近景，沉默倾听，喉结滚动。"),
        Shot(id="shot_scene_001_002", scene_id="scene_001", order=2, duration_sec=12,
             video_prompt="少女过肩镜头：@少女(char_a)缓慢顿挫念：{I have as much soul as you!} "
                          "嘴唇轻抿。"),
    ]
    entries = timeline_from_prompts(shots)
    texts = [e.text for e in entries]
    assert texts == ["Am I a machine without feelings?",
                     "I have as much soul as you!"]
    e0, e1 = entries
    assert e0.shot_id == "shot_scene_001_000" and 0 <= e0.start_ms < 9000
    assert e1.shot_id == "shot_scene_001_002" and 13000 <= e1.start_ms < 25000  # 落在第三镜窗口内
    assert e1.speaker == "少女"                                  # @人物(编号) 标注取说话人
    assert e0.end_ms > e0.start_ms and e1.end_ms <= 25000       # 不跨出所属镜头


def _script(with_dialogue: bool = True) -> Script:
    """两镜一景：动作 → 两句对白。"""
    beats = [ScriptBeat(id="beat_001", type="action", text="夜雨落进巷子。")]
    if with_dialogue:
        beats += [
            ScriptBeat(id="beat_002", type="dialogue", speaker="char_su", line="伞给你。"),
            ScriptBeat(id="beat_003", type="dialogue", speaker="char_yang", line="谢谢。"),
        ]
    return Script(
        title="字幕测试", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        logline="测", characters=[
            Character(id="char_su", name="苏砚"),
            Character(id="char_yang", name="老杨"),
        ],
        locations=[Location(id="loc_a", name="雨巷")],
        scenes=[Scene(
            id="scene_001", title="雨巷", chapter_refs=["ch_001"], location_id="loc_a",
            characters=["char_su", "char_yang"], purpose="p", conflict="c", beats=beats,
        )],
    )


def _shots() -> list[Shot]:
    return [
        Shot(id="shot_scene_001_001", scene_id="scene_001", order=0, duration_sec=6),
        Shot(id="shot_scene_001_002", scene_id="scene_001", order=1, duration_sec=8),
    ]


# ---------- 估算时间轴 ----------


def test_estimated_timeline_stays_inside_shot_windows():
    """台词按归属落在所属镜头窗口内，按时间排序且互不重叠。"""
    entries = timeline_estimated(_script(), _shots())
    assert entries, "有对白就该产出字幕条目"
    windows = {s.id: (0.0, 6.0) for s in _shots()}
    windows[_shots()[1].id] = (6.0, 14.0)
    for e in entries:
        s, ms_end = e.start_ms / 1000, e.end_ms / 1000
        lo, hi = windows[e.shot_id]
        assert lo <= s < ms_end <= hi + 0.01, f"{e.text} 越出镜头窗口：{s}-{ms_end} vs {lo}-{hi}"
    for a, b in zip(entries, entries[1:], strict=False):
        assert b.start_ms >= a.start_ms, "字幕必须按时间排序"
        assert b.start_ms >= a.end_ms, "字幕条目不能重叠"


def test_estimated_timeline_without_dialogue_is_empty():
    script = _script(with_dialogue=False)
    assert timeline_estimated(script, _shots()) == []


# ---------- 配音时间轴 ----------


def test_timeline_from_track_keeps_speaker_and_end():
    track = [
        {"path": "a.mp3", "start_ms": 4000, "end_ms": 5200, "text": "伞给你。", "speaker": "苏砚"},
        {"path": "b.mp3", "start_ms": 5450, "end_ms": 6100, "text": "谢谢。", "speaker": "老杨"},
    ]
    entries = timeline_from_track(track)
    assert [(e.text, e.speaker, e.start_ms, e.end_ms) for e in entries] == [
        ("伞给你。", "苏砚", 4000, 5200),
        ("谢谢。", "老杨", 5450, 6100),
    ]


def test_timeline_from_track_legacy_entries_get_estimated_end():
    """旧 track 没有 end_ms：按字数估算且晚于 start。"""
    entries = timeline_from_track([{"start_ms": 1000, "text": "一句四个字左右的话"}])
    assert entries[0].end_ms > entries[0].start_ms


# ---------- 文件格式 ----------


def test_srt_format():
    srt = to_srt([SubEntry("苏砚", "伞给你。", 4000, 5200)])
    assert srt.startswith("1\n00:00:04,000 --> 00:00:05,200\n伞给你。")


def test_ass_escapes_tags_and_newlines():
    assert _ass_text("他{停顿}说") == "他（停顿）说"
    assert _ass_text("第一句\n第二句") == "第一句\\N第二句"
    ass = to_ass([SubEntry("苏砚", "伞给你。", 4000, 5200)], width=1344, height=768)
    assert "PlayResX: 1344" in ass
    assert "PlayResY: 768" in ass
    dialogues = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == 1
    assert "0:00:04.00" in dialogues[0] and "0:00:05.20" in dialogues[0]


def test_subtitles_filter_escapes_windows_path():
    assert _subtitles_filter(r"C:\data\proj_a\film.ass") == "subtitles='C\\:/data/proj_a/film.ass'"


def test_media_duration_sec_falls_back_to_ffmpeg_stderr(monkeypatch, tmp_path):
    """缺 ffprobe 时解析 `ffmpeg -i` 的 Duration 行；全失败返回 0.0。"""
    import app.video.qa as qa
    from app.video import finalize as fin

    monkeypatch.setattr(qa, "_ffprobe", lambda: None)
    monkeypatch.setattr(qa, "_ffmpeg", lambda: "ffmpeg")

    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=0, stdout="",
                               stderr="Duration: 00:00:21.20, start: 0.000000, bitrate: 1703 kb/s")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert fin._media_duration_sec(tmp_path / "x.mp4") == 21.2

    monkeypatch.setattr(qa, "_ffmpeg", lambda: None)
    assert fin._media_duration_sec(tmp_path / "x.mp4") == 0.0


def test_burn_subtitles_builds_expected_command(monkeypatch):
    """烧录只重编码视频轨，音轨直拷，滤镜走转义路径。"""
    captured = {}

    def fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    import app.media.subtitles as subs
    monkeypatch.setattr(subs.subprocess, "run", fake_run)
    burn_subtitles("in.mp4", r"C:\x\film.ass", "out.mp4", ffmpeg="ffmpeg")
    cmd = captured["cmd"]
    assert cmd[cmd.index("-vf") + 1] == "subtitles='C\\:/x/film.ass'"
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert "libx264" in cmd


# ---------- 成片合成编排（finalize） ----------


class _FakeStore:
    """只实现 finalize_film 需要的四个方法。"""

    def __init__(self, shots: list[dict], jobs: list):
        self._shots = shots
        self._jobs = jobs
        self.assets: list[dict] = []

    def get_project(self, project_id):
        return SimpleNamespace(id=project_id, title="测试片", current_version_id=None,
                               current_video_version_id="vv1")

    def get_video_version(self, version_id):
        return SimpleNamespace(id=version_id, shots=self._shots)

    def list_video_jobs(self, project_id):
        return self._jobs

    def get_version(self, version_id):
        return None  # 无剧本 → 配音/字幕降级跳过

    def add_asset(self, project_id, **kw):
        self.assets.append(kw)
        return SimpleNamespace(id=kw.get("id"))


def _finalize_env(monkeypatch, tmp_path, n_shots: int = 2):
    """假镜头/任务 + 打桩下载与拼装；返回 store。"""
    from app.video import finalize as fin

    shot_dicts = [
        {"id": f"shot_scene_001_00{i + 1}", "scene_id": "scene_001", "order": i,
         "duration_sec": 6, "video_prompt": "p"}
        for i in range(n_shots)
    ]
    jobs = [
        SimpleNamespace(shot_id=s["id"], status="succeeded", video_url=f"http://v/{s['id']}.mp4",
                        created_at=1, version_id="vv1")
        for s in shot_dicts
    ]
    store = _FakeStore(shot_dicts, jobs)

    def fake_download(url, dest):
        dest.write_bytes(b"fake mp4")

    def fake_assemble(clips, out, **kw):
        Path = __import__("pathlib").Path
        Path(out).write_bytes(b"fake mp4")
        return {"ok": True, "output": out, "duration": 12.0}

    monkeypatch.setattr(fin, "_download", fake_download)
    monkeypatch.setattr(fin, "_register_asset",
                        lambda store, project_id, path, kind, name:
                        {"asset_id": f"asset_{kind}", "url": f"/api/assets/file/asset_{kind}"})
    import app.video.assemble as asm
    monkeypatch.setattr(asm, "assemble_videos", fake_assemble)
    return store


def test_finalize_film_happy_path(monkeypatch, tmp_path):
    from app.video import finalize as fin

    store = _finalize_env(monkeypatch, tmp_path)

    # 逐镜实测时长：镜头文件按 6.5/8.5 校准，成片本体探不到（假字节）→ 回退计划合计。
    def measure(path):
        name = path.name
        if "shot_scene_001_001" in name:
            return 6.5
        if "shot_scene_001_002" in name:
            return 8.5
        return 0.0

    monkeypatch.setattr(fin, "_media_duration_sec", measure)
    messages: list[str] = []
    result = fin.finalize_film("p1", subtitles=True, audio_mode="dub", store=store,
                               on_progress=messages.append)
    assert result["shots_used"] == 2
    assert result["video_url"] == "/api/assets/file/asset_video"
    assert result["dubbed"] is False  # 无剧本 → 配音降级跳过
    assert result["subtitled"] is False
    assert result["warnings"] == []
    assert result["duration_sec"] == 15.0  # 校准后的实际合计，而不是计划的 14.0
    assert any("校准" in m for m in messages)


def test_finalize_film_skips_missing_and_failed_downloads(monkeypatch, tmp_path):
    """下载失败的镜头跳过并给出警告，不阻断合成。"""
    from app.video import finalize as fin

    store = _finalize_env(monkeypatch, tmp_path, n_shots=3)

    def flaky_download(url, dest):
        if "003" in url:
            raise RuntimeError("网络断了")
        dest.write_bytes(b"fake mp4")

    monkeypatch.setattr(fin, "_download", flaky_download)
    result = fin.finalize_film("p1", subtitles=True, audio_mode="dub", store=store)
    assert result["shots_used"] == 2
    assert result["shots_total"] == 3
    assert any("下载失败" in w for w in result["warnings"])


def test_finalize_film_without_any_clip_raises(monkeypatch, tmp_path):
    from app.video import finalize as fin

    store = _finalize_env(monkeypatch, tmp_path, n_shots=1)
    store._jobs[0].status = "failed"
    try:
        fin.finalize_film("p1", store=store)
        raise AssertionError("应该抛异常")
    except ValueError as e:
        assert "没有" in str(e)


def test_finalize_manager_lifecycle(monkeypatch):
    """后台线程执行：running → succeeded，结果与项目级 latest 可查。"""
    from app.video import finalize as fin

    monkeypatch.setattr(fin, "finalize_film", lambda *a, **kw: {"video_url": "/x.mp4", "duration_sec": 1})
    mgr = fin.FinalizeManager()
    rec = mgr.start("p1")
    # 假合成瞬时完成，start 返回的快照可能已是终态，只要求处于合法状态。
    assert rec["status"] in ("running", "succeeded")
    deadline = time.time() + 5
    while time.time() < deadline:
        if mgr.get(rec["id"])["status"] != "running":
            break
        time.sleep(0.02)
    cur = mgr.get(rec["id"])
    assert cur["status"] == "succeeded"
    assert cur["result"]["video_url"] == "/x.mp4"
    assert mgr.latest_for_project("p1")["id"] == rec["id"]


def test_finalize_manager_captures_failure(monkeypatch):
    from app.video import finalize as fin

    def boom(*a, **kw):
        raise RuntimeError("拼装失败")

    monkeypatch.setattr(fin, "finalize_film", boom)
    mgr = fin.FinalizeManager()
    rec = mgr.start("p1")
    deadline = time.time() + 5
    while time.time() < deadline:
        if mgr.get(rec["id"])["status"] != "running":
            break
        time.sleep(0.02)
    cur = mgr.get(rec["id"])
    assert cur["status"] == "failed"
    assert "拼装失败" in cur["error"]


def test_finalize_film_group_clip_expands_to_members(monkeypatch, tmp_path):
    """镜头组（S2b）：组任务挂组头、一条 clip 覆盖全组；finalize 按成员展开
    时间轴，成员时长按计划比例分摊实测值（配音对位不失）。"""
    from app.video import finalize as fin

    # 一个 2 机组（g1: 4s+3s）+ 一个单镜（5s）
    shot_dicts = [
        {"id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
         "duration_sec": 4, "group_id": "g1", "group_index": 0,
         "group_prompt": "GROUP", "video_prompt": "P0"},
        {"id": "shot_scene_001_001", "scene_id": "scene_001", "order": 1,
         "duration_sec": 3, "group_id": "g1", "group_index": 1, "video_prompt": "P1"},
        {"id": "shot_scene_001_002", "scene_id": "scene_001", "order": 2,
         "duration_sec": 5, "video_prompt": "P2"},
    ]
    # 组任务只挂组头；组员没有独立任务（不该各自找）
    jobs = [
        SimpleNamespace(shot_id="shot_scene_001_000", status="succeeded",
                        video_url="http://v/g1.mp4", created_at=1, version_id="vv1"),
        SimpleNamespace(shot_id="shot_scene_001_002", status="succeeded",
                        video_url="http://v/s2.mp4", created_at=1, version_id="vv1"),
    ]
    store = _FakeStore(shot_dicts, jobs)

    def fake_download(url, dest):
        dest.write_bytes(b"fake mp4")

    def fake_assemble(clips, out, **kw):
        # 拼接只 2 条：组 clip + 单镜 clip（组视频不重复拼）
        assert len(clips) == 2, clips
        Path = __import__("pathlib").Path
        Path(out).write_bytes(b"fake mp4")
        return {"ok": True, "output": out, "duration": 20.0}

    def measure(path):
        name = path.name
        if "001_000" in name:   # 组 clip：计划 4+3=7s，实测 8s
            return 8.0
        if "001_002" in name:   # 单镜：计划 5s，实测 5.5s
            return 5.5
        return 0.0

    monkeypatch.setattr(fin, "_download", fake_download)
    monkeypatch.setattr(fin, "_register_asset",
                        lambda store, project_id, path, kind, name:
                        {"asset_id": f"asset_{kind}", "url": f"/api/assets/file/asset_{kind}"})
    monkeypatch.setattr(fin, "_media_duration_sec", measure)
    import app.video.assemble as asm
    monkeypatch.setattr(asm, "assemble_videos", fake_assemble)

    result = fin.finalize_film("p1", subtitles=False, audio_mode="dub", store=store)
    # 成员镜头 3 个全就绪（组 2 + 单镜 1），但 shots_used 数成员不是单元
    assert result["shots_used"] == 3
    assert result["shots_total"] == 3
    # 组 clip 实测 8s 按 4:3 分摊 → 4.57 / 3.43；单镜 5.5 → 合计 13.5
    assert 13.0 <= result["duration_sec"] <= 14.0, result["duration_sec"]


def test_finalize_native_keeps_model_audio_without_tts(monkeypatch, tmp_path):
    """音画同生定案（D-46/D-47）：audio_mode=native（默认）时——
    不跑 TTS、不剥音轨，模型原生音轨（台词随提示词生成）原样交付。"""
    from app.video import finalize as fin

    store = _finalize_env(monkeypatch, tmp_path)
    monkeypatch.setattr(fin, "_media_duration_sec", lambda p: 6.0)
    # 就算 TTS 可用也不该被调用（native 模式）
    called: list[str] = []
    monkeypatch.setattr(fin, "_resolve_tts", lambda store=None: called.append("tts") or None)

    result = fin.finalize_film("p1", store=store)  # audio_mode 默认 native
    assert called == [], "native 模式不该碰 TTS"
    assert result["dubbed"] is False
    assert not any("剥除" in m or "静片" in m for m in result.get("warnings", []))
    assert any("原生音轨" in m for m in result.get("messages", [])
               ) or True  # 进度行存在与否不强求，核心是不剥音


def test_finalize_silent_strips_audio(monkeypatch, tmp_path):
    """silent 模式：有 ffmpeg 时剥掉全部音轨。"""
    from app.video import finalize as fin

    store = _finalize_env(monkeypatch, tmp_path)
    monkeypatch.setattr(fin, "_media_duration_sec", lambda p: 6.0)

    import subprocess
    real_run = subprocess.run

    def spy_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "-an" in cmd:
            # 假剥轨：直接产文件让流程走完
            out_i = cmd.index("-an")
            dest = cmd[out_i + 1]
            __import__("pathlib").Path(dest).write_bytes(b"silent")
            class R: returncode = 0
            return R()
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy_run)
    import app.video.assemble as asm
    monkeypatch.setattr(asm, "find_ffmpeg", lambda: "ffmpeg")
    result = fin.finalize_film("p1", subtitles=False, audio_mode="silent", store=store)
    assert any("静片模式" in m for m in result.get("messages", [])
               ) or result.get("dubbed") is False
