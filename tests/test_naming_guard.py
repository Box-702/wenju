"""名层护栏的回归测试：deepseek 把显示名写成拼音 slug（2026-11-10 完整跑实测）。"""
from __future__ import annotations

import pytest

import app.pipeline.generation as gen
from app.pipeline.generation import Bible, ScenePlan, _bible_naming_issue


def _bible(names):
    return Bible(title="t", characters=[{"name": n} for n in names],
                 locations=[{"name": "后巷"}])


def _plan():
    return ScenePlan.model_validate({"scenes": [{
        "scene_id": "scene_001", "title": "第一场", "location": "后巷",
        "beats": [{"type": "action", "text": "林然推门。"}],
    }]})


def test_拼音名判定只咬中文输出():
    assert _bible_naming_issue(_bible(["lin_ran"]), "zh-CN") is not None
    assert _bible_naming_issue(_bible(["林然"]), "zh-CN") is None
    # 英文改编：拉丁名是正当的，不许误伤
    assert _bible_naming_issue(_bible(["Jay Gatsby"]), "en-US") is None
    # 混排（中文名+拼音备注）算合格：有汉字就是原著称呼
    assert _bible_naming_issue(_bible(["林然 lin"]), "zh-CN") is None


class _Avail:
    available = True


def test_名退化时有界重跑一次并采用修正版(monkeypatch):
    calls = {"bible": 0}

    def fake_bible(llm, settings, *, critique="", **kw):
        calls["bible"] += 1
        # 第一次交拼音名，第二次（带修正指令）交中文名
        if critique:
            return _bible(["林然"])
        return _bible(["lin_ran"])

    monkeypatch.setattr(gen, "_stage_bible", fake_bible)
    monkeypatch.setattr(gen, "_stage_scenes", lambda *a, **k: _plan())

    script, artifacts = gen.generate_script(
        _Avail(), None, title="雨夜", raw_text="凌晨三点，林然站在后巷。",
        adaptation_type="short_drama", language="zh-CN",
    )
    assert calls["bible"] == 2          # 恰好重跑一次，不多花
    assert [c.name for c in script.characters] == ["林然"]
    assert artifacts["naming_degraded"] is False


def test_重跑仍不合格则不再重试只记标记(monkeypatch):
    """有界：模型两次都没听话，就停下（带标记如实汇报），绝不无限重试烧钱。"""
    calls = {"bible": 0}

    def stubborn(llm, settings, **kw):
        calls["bible"] += 1
        return _bible(["lin_ran"])

    monkeypatch.setattr(gen, "_stage_bible", stubborn)
    monkeypatch.setattr(gen, "_stage_scenes", lambda *a, **k: _plan())

    _script, artifacts = gen.generate_script(
        _Avail(), None, title="雨夜", raw_text="凌晨三点。",
        adaptation_type="short_drama", language="zh-CN",
    )
    assert calls["bible"] == 2                      # 封顶一次重跑
    assert artifacts["naming_degraded"] is True     # 对话层可据此如实告知
