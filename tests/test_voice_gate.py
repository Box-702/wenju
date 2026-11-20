# =====================================================================
# test_voice_gate.py —— 声音闸门（2026-11-20，A4）
#
# 《替身v2》看片反馈「声音不清不楚、偶尔有声音偶尔没有」，实测坐实：
# 14 个成片里 4 个台词镜只有 2 个语音频段有能量——006_024 字幕写着
# 「辛苦了，喝点水吧」而音轨 mean −33.4dB（纯环境噪声），一路漏到成片。
# 视觉质检抽帧判画面，根本看不见声音。
#
# 三处修：
#   1. qa.voice_check：语音频段（200–3500Hz 带通）volumedetect，
#      mean ≤ −32dB 判「台词没出人声」（阈值按同一批成片校准）；
#   2. reroll_if_needed：台词镜先查声音；**纯声音缺陷**（画面合格）
#      走同参数重抽——改写没有着力点，出声是随机事件；
#   3. subtitles._shot_lead：估算字幕起始对齐 dialogue_at（dub 实测轨道
#      一直尊重它，估算轨道吞掉了 → 两种模式字幕位置不一致）。
# =====================================================================

from __future__ import annotations

from types import SimpleNamespace

from app.video import qa


# ---------------------------------------------------------------- 台词判定


def test_官方句式判台词_招牌引用不算():
    assert qa.prompt_has_dialogue("林远说话：「别紧张。」")
    assert qa.prompt_has_dialogue("镜头1（4秒）：掌柜念：「不出一月，定然还清。」")
    # 招牌/粉板文字不带冒号前缀 → 不是台词，不该被要求出人声
    assert not qa.prompt_has_dialogue("粉板上写下「孔乙己，十九文」")
    assert not qa.prompt_has_dialogue("他站起来，走到舱前俯视")


# ---------------------------------------------------------------- voice_check


def test_台词镜无声判fail有声判pass(tmp_path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")   # 本地存在即跳过下载分支
    orig_f, orig_m = qa._ffmpeg, qa._speech_band_mean_db
    try:
        qa._ffmpeg = lambda: "ffmpeg"
        qa._speech_band_mean_db = lambda p: -35.9      # 实测 006_024 的水平
        r = qa.voice_check(str(clip))
        assert r["verdict"] == "fail" and "-35.9" in r["note"]
        qa._speech_band_mean_db = lambda p: -19.1      # 实测 002_006 的水平
        assert qa.voice_check(str(clip))["verdict"] == "pass"
        # 边界：恰好 −32 不算无声（> 判据）
        qa._speech_band_mean_db = lambda p: -32.0
        assert qa.voice_check(str(clip))["verdict"] == "pass"
    finally:
        qa._ffmpeg, qa._speech_band_mean_db = orig_f, orig_m


def test_没音轨的台词镜判fail(tmp_path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    orig = (qa._ffmpeg, qa._speech_band_mean_db, qa._has_audio_stream)
    try:
        qa._ffmpeg = lambda: "ffmpeg"
        qa._speech_band_mean_db = lambda p: None       # 读不出 mean
        qa._has_audio_stream = lambda p: False        # 且确认没有音轨
        assert qa.voice_check(str(clip))["verdict"] == "fail"
        qa._has_audio_stream = lambda p: None         # 探测不可用 → 保守跳过
        assert qa.voice_check(str(clip))["verdict"] == "skipped"
    finally:
        qa._ffmpeg, qa._speech_band_mean_db, qa._has_audio_stream = orig


def test_没有ffmpeg按跳过不阻断():
    orig = qa._ffmpeg
    try:
        qa._ffmpeg = lambda: None
        assert qa.voice_check("http://v/x.mp4")["verdict"] == "skipped"
    finally:
        qa._ffmpeg = orig


# ---------------------------------------------------------------- reroll 接线


def _reroll_store(prompt: str, params: dict):
    job = SimpleNamespace(
        id="j1", project_id="p", shot_id="s", provider="seedance", model="m",
        prompt=prompt, params=params, version_id="v", cost_estimate=None,
        status="succeeded", video_url="http://v/0.mp4",
    )
    created, updates = [], []
    store = SimpleNamespace(
        get_video_job=lambda job_id: job,
        update_video_job=lambda job_id, **f: updates.append(f),
        create_video_job=lambda **kw: (created.append(kw), SimpleNamespace(id="j2"))[1],
    )
    return store, created, updates


def test_纯声音缺陷走同参数重抽不改写(monkeypatch):
    """画面合格 + 台词没出人声 → 改写没有着力点（出声是随机事件），
    同参数重 roll；构图降级规则帮不上这种病。"""
    monkeypatch.setattr(qa, "voice_check",
                        lambda url: {"verdict": "fail", "mean_db": -35.9,
                                     "note": "语音频段平均 -35.9dB，台词没出人声"})
    monkeypatch.setattr(qa, "qa_check",
                        lambda url, **kw: {"verdict": "pass", "issues": [], "frames": 5})
    monkeypatch.setattr(qa, "_group_segments", lambda job, store: [])
    store, created, updates = _reroll_store(
        "林远说话：「别紧张。」", {"duration_sec": 5})
    submitted = []
    new_id = qa.reroll_if_needed("j1", {"status": "succeeded"}, store=store,
                                 submit=lambda j: submitted.append(j.id),
                                 max_reroll=1)
    assert new_id == "j2"
    assert created[0]["prompt"] == "林远说话：「别紧张。」"   # 一字未改
    assert not created[0]["params"].get("qa_rewritten")
    # qa 结论里声音结果被记录
    qa_field = updates[0]["params"]["qa"]
    assert qa_field["voice"]["verdict"] == "fail" and qa_field["verdict"] == "fail"


def test_声音加画面双缺陷仍走改写路径(monkeypatch):
    monkeypatch.setattr(qa, "voice_check",
                        lambda url: {"verdict": "fail", "mean_db": -35.9, "note": "无声"})
    monkeypatch.setattr(qa, "qa_check",
                        lambda url, **kw: {"verdict": "fail",
                                           "issues": ["第2帧：手穿模"], "frames": 5})
    monkeypatch.setattr(qa, "_group_segments", lambda job, store: [])
    monkeypatch.setattr(qa, "rewrite_prompt_for_retry",
                        lambda p, i, **kw: "REWRITTEN")
    store, created, _ = _reroll_store("他说话：「来了。」", {"duration_sec": 5})
    assert qa.reroll_if_needed("j1", {"status": "succeeded"}, store=store,
                               submit=lambda j: None, max_reroll=1) == "j2"
    assert created[0]["prompt"] == "REWRITTEN"


def test_无台词镜不做声音检测(monkeypatch):
    """没有台词的镜头语音频段本来就只有环境声——检测它会把正常空镜全判 fail。"""
    calls: list[str] = []
    monkeypatch.setattr(qa, "voice_check",
                        lambda url: calls.append(url) or {"verdict": "pass",
                                                          "mean_db": -30.0, "note": ""})
    monkeypatch.setattr(qa, "qa_check",
                        lambda url, **kw: {"verdict": "pass", "issues": [], "frames": 5})
    monkeypatch.setattr(qa, "_group_segments", lambda job, store: [])
    store, created, updates = _reroll_store("他站起来，走到舱前俯视", {"duration_sec": 5})
    assert qa.reroll_if_needed("j1", {"status": "succeeded"}, store=store,
                               submit=lambda j: None, max_reroll=1) is None
    assert calls == []                       # 根本没测
    assert "voice" not in updates[0]["params"]["qa"]
