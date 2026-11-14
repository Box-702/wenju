# =====================================================================
# test_video_usage.py —— 出片用量摘要（D-53：界面零费用，只报资源量）
#
# 界面只显示三类创作者语言的数量：预期生成秒数、参考视频条数与总时长、
# 参考图张数。时长来源按可靠度：本机素材实测 > 接力锚溯源 > 只计数
# （宁可少报不编数）。
# =====================================================================

from __future__ import annotations

from types import SimpleNamespace

from app.video.usage import fallback_usage, job_usage, reference_usage, relay_durations


def test_reference_usage_接力锚溯源时长():
    params = {"reference_videos": ["http://v/a.mp4", "http://v/b.mp4"],
              "reference_images": ["img1", "img2"]}
    usage = reference_usage(params,
                            url_durations={"http://v/a.mp4": 4.0, "http://v/b.mp4": 6.0})
    assert usage == {"ref_video_count": 2, "ref_video_sec": 10.0, "ref_image_count": 2}


def test_reference_usage_拿不到时长只计数():
    params = {"reference_videos": ["http://v/unknown.mp4"], "reference_images": []}
    usage = reference_usage(params, url_durations={})
    assert usage["ref_video_count"] == 1
    assert usage["ref_video_sec"] is None   # 宁可少报不编数
    assert usage["ref_image_count"] == 0


def test_reference_usage_本机素材实测加接力溯源(monkeypatch):
    store = SimpleNamespace(get_asset=lambda aid: SimpleNamespace(content=b"mp4-bytes"))
    monkeypatch.setattr("app.video.finalize._media_duration_sec", lambda p: 3.5)
    params = {"reference_videos": ["/api/assets/file/asset_x", "http://v/relay.mp4"],
              "reference_images": ["img"]}
    usage = reference_usage(params, store=store,
                            url_durations={"http://v/relay.mp4": 2.5})
    assert usage["ref_video_count"] == 2
    assert usage["ref_video_sec"] == 6.0    # 3.5 实测 + 2.5 溯源
    assert usage["ref_image_count"] == 1


def test_job_usage_汇总生成秒数():
    params = {"duration_sec": 7, "reference_videos": ["http://v/a.mp4"],
              "reference_images": []}
    out = job_usage(params, url_durations={"http://v/a.mp4": 4.0})
    assert out == {"gen_sec": 7, "ref_video_count": 1,
                   "ref_video_sec": 4.0, "ref_image_count": 0}


def test_relay_durations_只收有时长的任务产物():
    jobs = [
        SimpleNamespace(video_url="http://v/a.mp4", params={"duration_sec": 6.0}),
        SimpleNamespace(video_url="http://v/b.mp4", params={}),          # 没时长不进映射
        SimpleNamespace(video_url=None, params={"duration_sec": 5}),     # 没产物不进映射
    ]
    assert relay_durations(jobs) == {"http://v/a.mp4": 6.0}


def test_fallback_usage_老任务只计数():
    out = fallback_usage({"duration_sec": 6, "reference_videos": ["http://v/a.mp4"],
                          "reference_images": ["i1", "i2"]})
    assert out == {"gen_sec": 6, "ref_video_count": 1,
                   "ref_video_sec": None, "ref_image_count": 2}
    assert fallback_usage({})["gen_sec"] is None
