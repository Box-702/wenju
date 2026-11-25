# =====================================================================
# test_voice_casting.py —— 音色规范（2026-11-20，A3）
#
# 《替身v2》看片反馈「声音不清不楚」，查到的事实：
#   · Seedance 2.0 没有 voice_id 字段——音色唯一通道是 content 里的
#     audio_url role=reference_audio（≤3 段、必须同时带图/视频），
#     而我们的适配器 _content() 漏了这一支，reference_audios 在 provider
#     边界被静默丢弃（文件头注释一直声称支持）；
#   · 自动选角（cast_script）每次现算、从不落库——没有「每角色一把嗓子」的
#     持久规范，改画风/换勾选池下一轮就换声；
#   · native 成片的人声完全由模型自由发挥，与资产库音色无关。
#
# 三处修：cast_and_persist 落库 / _content 补 audio_url 分支 /
# voice_audios_for 音色桥（提交侧合成入库一次，比对侧只读）。
# =====================================================================

from __future__ import annotations

from app.crew.director import _MOVEMENT_RE  # noqa: F401  (确保模块可导入)
from app.media.casting import cast_and_persist
from app.video.base import VideoJobParams
from app.video.providers.seedance import SeedanceProvider
from app.video.service import _intent_params_equal, _shot_speakers, voice_audios_for


# ---------------------------------------------------------------- 测试替身


class FakeStore:
    """只实现音色链路用到的方法（鸭子类型，与 store.py 同签名）。"""

    def __init__(self, style: dict | None = None):
        self.style = dict(style or {})
        self.assets: list = []
        self.merges: list[dict] = []

    def get_video_style(self, project_id):
        return dict(self.style)

    def merge_video_style(self, project_id, patch):
        self.style.update(patch)
        self.merges.append(patch)
        return dict(self.style)

    def list_assets(self, project_id, kind=None):
        return [a for a in self.assets if kind is None or a.kind == kind]

    def add_asset(self, project_id, **kw):
        a = type("A", (), kw)()
        self.assets.append(a)
        return a

    # build_voice_pool 的鸭子接口（BYOK 音色池三方法）：测试里不勾选、不克隆
    def enabled_voice_pool(self):
        return []

    def list_voice_pool(self):
        return []

    def list_user_voices(self):
        return []


class _Asset:
    def __init__(self, name, url, kind="audio"):
        self.name, self.url, self.kind = name, url, kind


def _script():
    from app.domain import (
        Character,
        Location,
        Scene,
        Script,
        ScriptBeat,
        Source,
    )

    return Script(
        title="t", version="1.0", language="zh-CN", logline="甲乙对话。",
        source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[
            Character(id="char_001", name="林远", gender="male"),
            Character(id="char_002", name="苏晴", gender="female"),
        ],
        locations=[Location(id="loc_001", name="书房")],
        scenes=[Scene(
            id="scene_001", title="第一场", chapter_refs=["ch_001"],
            location_id="loc_001", characters=["char_001", "char_002"],
            purpose="p", conflict="c",
            beats=[
                ScriptBeat(id="beat_001", type="dialogue", speaker="char_001", line="你来了。"),
                ScriptBeat(id="beat_002", type="dialogue", speaker="char_002", line="我来了。"),
            ],
        )],
    )


# ---------------------------------------------------------------- 选角落库


def test_自动选角落库_人工指定的不动():
    st = FakeStore({"voice_casting": {"char_001": "female-yujie"},  # 人工指定
                    "character_genders": {"char_001": "female"}})
    cast = cast_and_persist(st, "proj_x", _script())
    saved = st.style["voice_casting"]
    assert saved["char_001"] == "female-yujie"      # 人工的那条一个字没动
    assert saved.get("char_002")                    # 没指定过的补上了
    assert cast.voices_by_id["char_002"] == saved["char_002"]


def test_选角落库幂等_第二次不写库():
    st = FakeStore({})
    cast_and_persist(st, "proj_x", _script())
    first = len(st.merges)
    cast_and_persist(st, "proj_x", _script())
    assert len(st.merges) == first                  # 已落库 → 无新增写入


# ---------------------------------------------------------------- Seedance 通道


def _prov() -> SeedanceProvider:
    return SeedanceProvider(api_key="k")


def test_参考音频进content_且必须带图():
    p = _prov()
    items = p._content("提示词", VideoJobParams(
        reference_images=["https://x/sheet.png"],
        reference_audios=["data:audio/mpeg;base64,AAAA"]))
    audio = [i for i in items if i.get("type") == "audio_url"]
    assert len(audio) == 1 and audio[0]["role"] == "reference_audio"


def test_没有图或视频时不带音频_2_0硬规矩():
    """方舟 2.0：不可单独输入音频，至少要 1 张参考图或 1 个参考视频——
    不带视觉素材硬塞 audio 会被建任务直接拒，宁可不带。"""
    p = _prov()
    items = p._content("提示词", VideoJobParams(
        reference_audios=["data:audio/mpeg;base64,AAAA"]))
    assert not [i for i in items if i.get("type") == "audio_url"]


def test_参考音频最多三条():
    p = _prov()
    items = p._content("提示词", VideoJobParams(
        reference_images=["https://x/a.png"],
        reference_audios=[f"data:audio/mpeg;base64,{c}" for c in "ABCDEF"]))
    assert len([i for i in items if i.get("type") == "audio_url"]) == 3


# ---------------------------------------------------------------- 说话人解析


def test_说话人按dialogue_ref解析_组内全收():
    script = _script()
    plan = [
        {"id": "shot_a", "scene_id": "scene_001", "group_id": "g1",
         "group_index": 0, "dialogue_ref": "beat_001"},
        {"id": "shot_b", "scene_id": "scene_001", "group_id": "g1",
         "group_index": 1, "dialogue_ref": "beat_002"},
        {"id": "shot_c", "scene_id": "scene_001", "group_id": "",
         "dialogue_ref": None},
    ]
    # 组头提交 → 整组说话人都要（组提示词覆盖全组机位）
    assert _shot_speakers(plan, "shot_a", script) == ["char_001", "char_002"]
    # 单镜提交 → 只有自己的
    assert _shot_speakers(plan, "shot_c", script) == []


# ---------------------------------------------------------------- 音色桥门槛


def test_不支持音频的厂商不回填():
    st = FakeStore({})
    plan = [{"id": "shot_a", "scene_id": "scene_001", "dialogue_ref": "beat_001"}]
    assert voice_audios_for(st, "p", plan, "shot_a", _script(),
                            provider="kling", synthesize=False) == []


def test_显式给过reference_audios不覆盖():
    st = FakeStore({})
    plan = [{"id": "shot_a", "scene_id": "scene_001", "dialogue_ref": "beat_001"}]
    got = voice_audios_for(st, "p", plan, "shot_a", _script(),
                           provider="seedance",
                           stored_params={"reference_audios": ["/api/assets/file/x"]},
                           synthesize=False)
    assert got == []


def test_比对侧不合成不写库():
    """synthesize=False：没有缓存样音时返回空，且绝不新增 merge/add_asset。"""
    st = FakeStore({})
    plan = [{"id": "shot_a", "scene_id": "scene_001", "dialogue_ref": "beat_001"}]
    got = voice_audios_for(st, "p", plan, "shot_a", _script(),
                           provider="seedance", synthesize=False)
    assert got == []
    assert st.assets == []          # 没合成任何样音


def test_比对侧复用缓存样音():
    st = FakeStore({})
    # 先让提交侧的缓存命中路径：手工塞一条已存在的样音
    cast = cast_and_persist(st, "p", _script())
    cid, vid = "char_001", cast.voices_by_id["char_001"]
    st.assets.append(_Asset(f"音色样音::{cid}::{vid}", f"/api/assets/file/{cid}"))
    plan = [{"id": "shot_a", "scene_id": "scene_001", "dialogue_ref": "beat_001"}]
    got = voice_audios_for(st, "p", plan, "shot_a", _script(),
                           provider="seedance", synthesize=False)
    assert got == [f"/api/assets/file/{cid}"]


# ---------------------------------------------------------------- 幂等豁免


def test_上线前的老任务不因音色键判成新意图():
    """老任务 params 没有 reference_audios（None），比对侧现在算得出样音——
    必须仍判同意图，否则重开批次会把上线前的成功镜头重新烧一遍。"""
    assert _intent_params_equal(
        {"duration_sec": 5, "resolution": "768P"},
        {"duration_sec": 5, "resolution": "768P",
         "reference_audios": ["/api/assets/file/a"]},
    )


def test_真换过音色判成新意图():
    assert not _intent_params_equal(
        {"duration_sec": 5, "reference_audios": ["/api/assets/file/old"]},
        {"duration_sec": 5, "reference_audios": ["/api/assets/file/new"]},
    )


# ---------------------------------------------------------------- 台词样本（2026-11-25）


def _fake_tts(monkeypatch, calls: list[dict]):
    class TTS:
        def synthesize(self, text, *, voice_id="", speed=1.0, timeout=120.0):
            calls.append({"text": text, "voice_id": voice_id})
            return b"ID3-fake-mp3-bytes" * 20

    monkeypatch.setattr("app.video.finalize._resolve_tts", lambda store: TTS())


def test_样音用角色台词且缓存键带文本摘要(monkeypatch):
    """用户口径：先出好声音再提交——样音用角色自己的台词，换文本重新合成。"""
    from app.video import service as svc

    calls: list[dict] = []
    _fake_tts(monkeypatch, calls)
    st = FakeStore({})
    url = svc._voice_sample(st, "p", "char_001", "male-qn-qingse",
                            text="你来了。我等你很久了。")
    assert calls and calls[0]["text"] == "你来了。我等你很久了。"
    tag = st.assets[0].name
    assert tag.startswith("音色样音::char_001::male-qn-qingse::")
    assert len(tag.rsplit("::", 1)[-1]) == 8
    # 同 (角色, 音色, 文本) 命中缓存不再烧 TTS
    assert svc._voice_sample(st, "p", "char_001", "male-qn-qingse",
                             text="你来了。我等你很久了。") == url
    assert len(calls) == 1
    # 换台词文本 → 新缓存键、重新合成
    svc._voice_sample(st, "p", "char_001", "male-qn-qingse", text="另一句台词。")
    assert len(calls) == 2 and len(st.assets) == 2


def test_角色台词收集按顺序并截断():
    from app.video.service import _character_lines

    script = _script()
    assert _character_lines(script, "char_001") == "你来了。"
    assert _character_lines(script, "char_999") == ""


def test_样音字符预算随说话人数收缩():
    """MiniMax 参考音频合计 ≤15s（2026-11-25 实测 2013）；按人数分预算。"""
    from app.video.service import _sample_char_budget

    assert _sample_char_budget(1) == 40
    assert _sample_char_budget(2) == 26
    assert _sample_char_budget(3) == 16
    # 粗估：中文 TTS 约 4.5 字/秒，两人单元合计不超过 15 秒
    assert _sample_char_budget(2) * 2 / 4.5 <= 15
    assert _sample_char_budget(3) * 3 / 4.5 <= 15
