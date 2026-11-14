# =====================================================================
# test_finalize_version.py —— 成片合成的版本隔离（P1 收口 2026-11-14）
#
# 病根：_latest_succeeded_url 只按 shot_id + created_at 取最近成片，不看
# version_id。旧版本重新合成时会拼进**新版本**的成片（或反过来）——内容
# 与镜头方案对不上，用户从成片里看不出来。
#
# 兼容口径（与 _auto_references 同一份）：单镜手提交/对账回写的任务常不带
# version_id，无法证明是别代 → 放行；明确归属其他版本 → 绝不混入。
# =====================================================================

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.video.finalize import _latest_succeeded_url


T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _job(shot_id: str, url: str, *, version_id: str | None = None,
         status: str = "succeeded", at: datetime = T0):
    return type("J", (), {
        "shot_id": shot_id, "video_url": url, "status": status,
        "version_id": version_id, "created_at": at,
    })()


def test_本版本成片优先_即使更旧():
    """同版本命中后不再看无归属任务——哪怕后者时间更新（归属比新旧可信）。"""
    jobs = [
        _job("shot_a", "http://v/same.mp4", version_id="vver_new", at=T0),
        _job("shot_a", "http://v/legacy.mp4", version_id=None,
             at=T0 + timedelta(minutes=5)),
    ]
    assert _latest_succeeded_url(jobs, "shot_a", "vver_new") == "http://v/same.mp4"


def test_其他版本的成片绝不混入():
    """明确属于别代的成片被排除；本代没有时回落到无归属的历史任务。"""
    jobs = [
        _job("shot_a", "http://v/other.mp4", version_id="vver_old",
             at=T0 + timedelta(minutes=9)),
        _job("shot_a", "http://v/legacy.mp4", version_id=None, at=T0),
    ]
    assert _latest_succeeded_url(jobs, "shot_a", "vver_new") == "http://v/legacy.mp4"


def test_只有其他版本成片时返回None():
    """别代成片不是兜底——宁可报「没有成片」也不能拼错内容。"""
    jobs = [_job("shot_a", "http://v/other.mp4", version_id="vver_old", at=T0)]
    assert _latest_succeeded_url(jobs, "shot_a", "vver_new") is None


def test_无归属任务取最近():
    jobs = [
        _job("shot_a", "http://v/old.mp4", version_id=None, at=T0),
        _job("shot_a", "http://v/new.mp4", version_id=None,
             at=T0 + timedelta(minutes=1)),
    ]
    assert _latest_succeeded_url(jobs, "shot_a", "vver_new") == "http://v/new.mp4"


def test_不传版本_保持旧口径取全局最近():
    """显式不传 version_id（兼容旧调用）：按 created_at 取最近，不过滤。"""
    jobs = [
        _job("shot_a", "http://v/a.mp4", version_id="vver_old", at=T0),
        _job("shot_a", "http://v/b.mp4", version_id="vver_new",
             at=T0 + timedelta(minutes=1)),
    ]
    assert _latest_succeeded_url(jobs, "shot_a") == "http://v/b.mp4"


def test_失败任务与非本镜任务不参与():
    jobs = [
        _job("shot_a", "http://v/failed.mp4", version_id="vver_new", status="failed"),
        _job("shot_b", "http://v/other_shot.mp4", version_id="vver_new"),
    ]
    assert _latest_succeeded_url(jobs, "shot_a", "vver_new") is None
