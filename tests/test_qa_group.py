# =====================================================================
# test_qa_group.py —— 镜头组的成片质检：抽帧密度与缺陷定位（PLAN-shot-group S2）
#
# 两件事：
#   1. 抽帧数按片长放大——固定 5 帧对 15 秒的镜头组太稀，整个机位都可能
#      没抽到，缺陷就这么漏过去了；
#   2. 缺陷定位到组内那一机位——重 roll 时只降级坏的那一段，其余机位逐字
#      保留（整段降级 = 白花钱且下一版更差）。
# =====================================================================

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------- 抽帧密度 ----------


def test_抽帧数按片长放大(monkeypatch):
    from app.video import qa

    monkeypatch.setattr(qa, "extract_frames", lambda url, count=None: [])
    base = qa.frame_count_for(None)
    assert base >= 3

    short = qa.frame_count_for(4)      # 单镜：取配置值
    long_group = qa.frame_count_for(15)  # 镜头组：抽得更密
    assert long_group > short
    assert 7 <= long_group <= 9
    assert qa.frame_count_for(60) == 9, "上限 9 帧，别把质检跑成烧钱"


def test_qa_check_按片长决定抽帧数(monkeypatch):
    from app.video import qa

    seen: dict = {}

    def _fake(url, count=None):
        seen["count"] = count
        return []

    monkeypatch.setattr(qa, "extract_frames", _fake)
    qa.qa_check("http://v/0.mp4", duration_sec=15)
    assert seen["count"] and seen["count"] >= 7


# ---------- 缺陷定位到机位段 ----------


GROUP_PROMPT = (
    "环境锚：旧当铺，木柜台。人物锚：客人短发灰衣。"
    "[Shot 1] 0-4s，客人俯身把铜钱一枚枚排在柜台上。"
    "[Shot 2] At 00:04.000, cuts to 掌柜眯眼打量。"
)


def _segments():
    from app.video.qa import _group_segments

    job = SimpleNamespace(
        params={"group_member_ids": ["shot_a_000", "shot_a_001"]},
        version_id="v1",
    )
    version = SimpleNamespace(shots=[
        {"id": "shot_a_000", "duration_sec": 4.0},
        {"id": "shot_a_001", "duration_sec": 3.0},
    ])
    store = SimpleNamespace(get_video_version=lambda vid: version)
    return _group_segments(job, store)


def test_组内机位段时间轴():
    segs = _segments()
    assert [(sid, round(s, 1), round(e, 1)) for sid, s, e in segs] == [
        ("shot_a_000", 0.0, 4.0),
        ("shot_a_001", 4.0, 7.0),
    ]


def test_缺陷定位到具体机位段():
    from app.video.qa import _locate_bad_segments

    segs = _segments()
    # 5 帧均匀抽在 7 秒里：第 5 帧 ≈ 6.3 秒 → 第二段（4–7s）
    bad = _locate_bad_segments(["第5帧：手指畸变"], segs, 7.0, 5)
    assert set(bad) == {1}
    # 第 1 帧 ≈ 0.7 秒 → 第一段（0–4s）
    bad = _locate_bad_segments(["第1帧：手指畸变"], segs, 7.0, 5)
    assert set(bad) == {0}


def test_只降级坏的那一段其余逐字保留(monkeypatch):
    from app.video import qa

    monkeypatch.setattr(qa, "_llm_rewrite_segment", lambda p, b, i: None)
    segs = _segments()
    bad = qa._locate_bad_segments(["第5帧：手指畸变"], segs, 7.0, 5)
    out = qa.rewrite_prompt_for_retry(
        GROUP_PROMPT, ["第5帧：手指畸变"], segments=segs, duration_sec=7.0, frame_count=5,
    )
    # 第一段逐字未动
    assert "[Shot 1] 0-4s，客人俯身把铜钱一枚枚排在柜台上。" in out
    # 第二段被降级
    assert "指尖级动作" in out
    # 锚没丢
    assert "环境锚：旧当铺，木柜台。" in out
    assert "人物锚：客人短发灰衣。" in out


def test_单镜仍走整段改写(monkeypatch):
    """没有组段信息 → 退回原来的整段改写（单镜行为不变）。"""
    from app.video import qa

    monkeypatch.setattr(qa, "_llm_rewrite", lambda p, i: None)
    out = qa.rewrite_prompt_for_retry("一段普通提示词", ["第2帧：手穿模"])
    assert "遮挡" in out or "接触" in out


def test_定位不到时退回整段改写(monkeypatch):
    """帧号异常定位不到段 → 整段处理，不静默什么都不做。"""
    from app.video import qa

    monkeypatch.setattr(qa, "_llm_rewrite", lambda p, i: None)
    segs = _segments()
    out = qa.rewrite_prompt_for_retry(
        GROUP_PROMPT, ["缺陷：手穿模"], segments=segs, duration_sec=7.0, frame_count=5,
    )
    assert "遮挡" in out or "接触" in out


@pytest.mark.parametrize("bad_frames", [[1], [5]])
def test_抽帧与定位对得上(monkeypatch, bad_frames):
    """端到端一点：抽 N 帧、每帧报错，定位到的段必须落在 0..1。"""
    from app.video import qa

    segs = _segments()
    issues = [f"第{i}帧：手指畸变" for i in bad_frames]
    bad = qa._locate_bad_segments(issues, segs, 7.0, 5)
    assert all(0 <= k < len(segs) for k in bad)
