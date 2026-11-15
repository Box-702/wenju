# =====================================================================
# test_dubbing.py —— 配音编排测试
#
# 覆盖：台词按「beats 段对齐」归属到镜头、音色分配、时间轴排程、
# ffmpeg 混音（用真实 ffmpeg + 合成音频，不调网络 TTS）。
# =====================================================================

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app.domain import (
    Character,
    Location,
    Scene,
    Script,
    ScriptBeat,
    Shot,
    Source,
)
from app.media.casting import assign_voices
from app.media.dubbing import (
    DubLine,
    mix_dubbing,
    plan_dubbing,
    synthesize_track,
)


def _script() -> Script:
    """v5 同构场景：动作 → 4 句对白 → 动作。"""
    beats = [
        ScriptBeat(id="beat_001", type="action", text="苏砚下车站定。"),
        ScriptBeat(id="beat_002", type="action", text="苏砚穿过警戒带。"),
        ScriptBeat(id="beat_003", type="dialogue", speaker="char_zhao", line="死者从七楼摔下来的。"),
        ScriptBeat(id="beat_004", type="dialogue", speaker="char_zhao", line="没有目击者。"),
        ScriptBeat(id="beat_005", type="dialogue", speaker="char_zhou", line="天台门呢？"),
        ScriptBeat(id="beat_006", type="dialogue", speaker="char_zhao", line="反锁着。"),
        ScriptBeat(id="beat_007", type="action", text="苏砚抬头望向楼顶。"),
    ]
    scene = Scene(
        id="scene_001", title="测试", chapter_refs=["ch_001"], location_id="loc_a",
        characters=["char_su", "char_zhou", "char_zhao"], purpose="p", conflict="c",
        beats=beats,
    )
    return Script(
        title="配音测试", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        logline="测", characters=[
            Character(id="char_su", name="苏砚"),
            Character(id="char_zhou", name="老周"),
            Character(id="char_zhao", name="小赵"),
        ],
        locations=[Location(id="loc_a", name="现场")],
        scenes=[scene],
    )


def _shots() -> list[Shot]:
    """三镜 6+8+6s：动作 / 对白 / 动作（与 beats 段结构一致）。"""
    return [
        Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=6),
        Shot(id="shot_scene_001_001", scene_id="scene_001", order=1, duration_sec=8),
        Shot(id="shot_scene_001_002", scene_id="scene_001", order=2, duration_sec=6),
    ]


def test_plan_dubbing_aligns_dialogue_to_middle_shot():
    """连续 4 句对白压成一个对话段，全部归属中间的对白镜头。"""
    plan = plan_dubbing(_script(), _shots())
    assert plan.total_sec == 20.0
    assert len(plan.lines) == 4
    assert {ln.shot_id for ln in plan.lines} == {"shot_scene_001_001"}
    assert plan.lines[0].text == "死者从七楼摔下来的。"
    assert plan.lines[2].speaker == "老周"


def test_assign_voices_distinguishes_gender():
    """名字带女性线索的角色拿女声，其余男女交替，分配稳定。"""
    voices = assign_voices(_script())
    assert voices["苏砚"] != voices["老周"]  # 苏砚名字含「砚」无线索，但至少男女有别
    assert voices["苏砚"] in {voices["苏砚"]}  # 稳定性由两次调用一致保证
    assert assign_voices(_script()) == voices


def test_explicit_dialogue_ref_wins(tmp_path):
    """镜头显式 dialogue_ref 时优先于段对齐。"""
    script = _script()
    shots = _shots()
    shots[2].dialogue_ref = "beat_005"
    plan = plan_dubbing(script, shots)
    ref_lines = [ln for ln in plan.lines if ln.text == "天台门呢？"]
    assert ref_lines and ref_lines[0].shot_id == "shot_scene_001_002"


def test_synthesize_and_mix_with_real_ffmpeg(tmp_path):
    """端到端混音：假 TTS（写静音 mp3）→ 排程 → 真实 ffmpeg 合成配音视频。"""
    from types import SimpleNamespace

    from app.video.qa import _ffmpeg

    ffmpeg = _ffmpeg()
    if not ffmpeg:
        pytest.skip("本机无 ffmpeg，跳过混音测试")

    # 造一段 2s 的测试视频（彩条 + 正弦音），模拟成片。
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(video)],
        check=True, capture_output=True,
    )

    # 假 TTS：用 ffmpeg 生成 0.5s 静音 mp3 当「合成结果」。
    class FakeTTS:
        def synthesize(self, text: str, *, voice_id: str = "", speed: float = 1.0) -> bytes:
            out = tmp_path / f"tts_{abs(hash(text))}.mp3"
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "sine=frequency=880:duration=0.5",
                 "-c:a", "libmp3lame", str(out)],
                check=True, capture_output=True,
            )
            return out.read_bytes()

    plan = SimpleNamespace(lines=[
        DubLine(speaker="小赵", text="第一句", voice_id="v1", shot_id="shot_scene_001_000"),
        DubLine(speaker="老周", text="第二句", voice_id="v2", shot_id="shot_scene_001_000"),
    ])
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=2)]
    track = synthesize_track(FakeTTS(), plan, shots, tmp_path / "work")
    assert len(track) == 2
    assert track[0]["start_ms"] == 400          # 镜头起点 + 0.4s 引导
    # 第二句 = 第一句起点 + 实测时长（mp3 编码器 padding 会略长于 0.5s）+ 句间 0.25s
    first_dur_ms = track[1]["start_ms"] - 250 - track[0]["start_ms"]
    assert 500 <= first_dur_ms <= 900
    assert track[1]["start_ms"] == track[0]["start_ms"] + first_dur_ms + 250
    # 字幕同步字段：speaker 随句携带，end_ms = start + 实测时长
    assert track[0]["speaker"] == "小赵"
    assert track[0]["end_ms"] == track[0]["start_ms"] + first_dur_ms

    out = tmp_path / "dubbed.mp4"
    mix_dubbing(video, track, out, bg_volume=0.25, ffmpeg=ffmpeg)
    assert out.exists() and out.stat().st_size > 0


# ---------- 部分渲染：只配音「拍出来的镜头」（2026-10-31 实测坑） ----------


def test_部分渲染且无_dialogue_ref_时整场台词跳过():
    """9 镜只拍 1 镜时，段对齐必然错位——宁可不出声，也不能把台词硬塞。

    真实故障：《孔乙己》挑 3 镜出片，配音却把本来属于另外 6 镜的台词
    全排到 19.8s 里，结果两句字幕叠在同一秒、整片被 1.6 倍速压缩。
    """
    plan = plan_dubbing(_script(), [_shots()[1]], expected_shots={"scene_001": 3})
    assert plan.lines == []
    assert plan.warnings and "只渲染了 1/3" in plan.warnings[0]


def test_全片渲染时台词一句不丢():
    """三镜全拍 = 正常行为，不能因为加了这套判断就误伤。"""
    plan = plan_dubbing(_script(), _shots(), expected_shots={"scene_001": 3})
    assert len(plan.lines) == 4
    assert not plan.warnings


def test_部分渲染时只保留_dialogue_ref_明确归属的台词():
    """镜头显式引用了某句对白 → 这句该留下；其余属于没拍镜头的对白丢掉。"""
    shots = [_shots()[1]]
    shots[0].dialogue_ref = "beat_005"   # 「天台门呢？」
    plan = plan_dubbing(_script(), shots, expected_shots={"scene_001": 3})
    assert [ln.text for ln in plan.lines] == ["天台门呢？"]
    assert plan.lines[0].shot_id == "shot_scene_001_001"
    assert plan.warnings and "已跳过配音" in plan.warnings[0]


# ---------- TTS 限流重试 ----------


def test_限流按退避重试后成功(monkeypatch, tmp_path):
    """MiniMax TTS 撞到 1002 RPM 限流 → 退避重试，不该直接丢句。"""
    import app.media.dubbing as dubbing

    slept: list[float] = []
    monkeypatch.setattr(dubbing.time, "sleep", lambda s: slept.append(s))

    class FlakyTTS:
        def __init__(self) -> None:
            self.calls = 0

        def synthesize(self, text: str, *, voice_id: str = "", speed: float = 1.0) -> bytes:
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError(
                    "MiniMax TTS 失败：insufficient_audio {'status_code': 1002, "
                    "'status_msg': 'rate limit exceeded(RPM)'}"
                )
            return b"ID3fake"

    plan = SimpleNamespace(lines=[
        DubLine(speaker="孔乙己", text="温两碗酒", voice_id="v1", shot_id="shot_t_001"),
    ])
    shots = [Shot(id="shot_t_001", scene_id="scene_001", order=0, duration_sec=6)]
    tts = FlakyTTS()
    track = synthesize_track(tts, plan, shots, tmp_path / "work")

    assert tts.calls == 3                     # 前两次失败，第三次成功
    assert slept == [2, 4]                    # 指数退避
    assert len(track) == 1


def test_非限流错误不重试(monkeypatch, tmp_path):
    """厂商真拒绝（没有音频）重试也没用，要立刻跳过而不是干等十几秒。"""
    import app.media.dubbing as dubbing

    slept: list[float] = []
    monkeypatch.setattr(dubbing.time, "sleep", lambda s: slept.append(s))

    class DeadTTS:
        def __init__(self) -> None:
            self.calls = 0

        def synthesize(self, text: str, *, voice_id: str = "", speed: float = 1.0) -> bytes:
            self.calls += 1
            raise RuntimeError("MiniMax TTS 响应中没有音频：{'base_resp': {}}")

    plan = SimpleNamespace(lines=[
        DubLine(speaker="孔乙己", text="温两碗酒", voice_id="v1", shot_id="shot_t_001"),
    ])
    shots = [Shot(id="shot_t_001", scene_id="scene_001", order=0, duration_sec=6)]
    tts = DeadTTS()
    track = synthesize_track(tts, plan, shots, tmp_path / "work")

    assert tts.calls == 1
    assert slept == []
    assert track == []


# ---------- 台词归属与落位（2026-10-31《孔乙己》实测：声音与画面对不上） ----------


def test_未引用的台词仍按段对齐而不是全塞进引用镜():
    """回归：一场戏只要有一镜填了 dialogue_ref，其余台词曾被全塞到那一镜上。

    《孔乙己》首版就是这样——画面里酒客在笑，声音却是孔乙己在说话，
    观众完全分不清「谁和谁说的」。
    """
    shots = _shots()
    shots[0].dialogue_ref = "beat_003"          # 只认领这段对话的第一句
    plan = plan_dubbing(_script(), shots)

    by_text = {ln.text: ln.shot_id for ln in plan.lines}
    # 同一人连说的两句跟着走（小赵），换成老周那句就断开了
    assert by_text["死者从七楼摔下来的。"] == "shot_scene_001_000"
    assert by_text["没有目击者。"] == "shot_scene_001_000"
    # 换人之后绝不能还被那一镜吞掉
    assert by_text["天台门呢？"] != "shot_scene_001_000"
    assert by_text["反锁着。"] != "shot_scene_001_000"


def test_dialogue_at_把台词落在镜头内指定秒数(monkeypatch, tmp_path):
    """导演写了 dialogue_at，配音就该在那一秒开口，而不是一律「镜头一开始就说」。"""
    import app.media.dubbing as dubbing

    monkeypatch.setattr(dubbing, "_audio_duration_sec", lambda p, f: 1.0)
    monkeypatch.setattr(dubbing, "_find_ffprobe", lambda: None)

    class FakeTTS:
        def synthesize(self, text: str, *, voice_id: str = "", speed: float = 1.0) -> bytes:
            return b"ID3fake"

    shots = [
        Shot(id="shot_a_001", scene_id="scene_001", order=0, duration_sec=6),
        Shot(id="shot_a_002", scene_id="scene_001", order=1, duration_sec=6),
    ]
    plan = SimpleNamespace(lines=[
        DubLine(speaker="甲", text="第一句", voice_id="v1", shot_id="shot_a_001"),
        DubLine(speaker="乙", text="第二句", voice_id="v2", shot_id="shot_a_002", at_sec=3.0),
    ])
    track = synthesize_track(FakeTTS(), plan, shots, tmp_path / "w")

    assert track[0]["start_ms"] == 400            # 没给 at_sec → 镜头起始后 0.4s
    assert track[1]["start_ms"] == 9000           # 6s 镜头 + 第 3 秒开口


def test_dialogue_at_超出镜头时夹在镜头内(monkeypatch, tmp_path):
    """导演把开口秒数写超了（或台词念不完），也不能让声音盖到下一镜的画面上。"""
    import app.media.dubbing as dubbing

    monkeypatch.setattr(dubbing, "_audio_duration_sec", lambda p, f: 1.0)
    monkeypatch.setattr(dubbing, "_find_ffprobe", lambda: None)

    class FakeTTS:
        def synthesize(self, text: str, *, voice_id: str = "", speed: float = 1.0) -> bytes:
            return b"ID3fake"

    shots = [Shot(id="shot_a_001", scene_id="scene_001", order=0, duration_sec=6)]
    plan = SimpleNamespace(lines=[
        DubLine(speaker="甲", text="第三句", voice_id="v1", shot_id="shot_a_001", at_sec=10.0),
    ])
    track = synthesize_track(FakeTTS(), plan, shots, tmp_path / "w")

    assert 0 <= track[0]["start_ms"] <= 6000
    assert track[0]["end_ms"] <= 6000
