# =====================================================================
# test_video_resume.py —— 进程重启后接管孤儿任务
#
# 病根：视频任务的轮询线程活在进程内存里。进程一重启，数据库里那条记录还停在
# `generating`，而没有任何人再去更新它——服务商那边其实早就出片了。用户看到
# 「一直生成中」，重试又会重复提交、重复计费。
#
# 这里锁住四条：
#   1. 接管只从轮询开始，**绝不 create_job**（否则两条片子两份钱）；
#   2. 接管是幂等的（同一个任务不会挂两个线程）；
#   3. 没有服务商任务号的（提交到一半死的）判失败并说明，不留着转圈；
#   4. 服务商配置丢了也判失败，而不是静默卡住。
# =====================================================================

from __future__ import annotations

import time

import pytest

from app.video import queue as queue_mod
from app.video.base import VideoJobResponse, VideoJobStatus
from app.video.queue import VideoJobManager
from app.video.sweeper import resume_orphans


class FakeProvider:
    """只实现接管需要的两个方法；create_job 被调用即算失败（用了不该用的路径）。"""

    name = "fake"

    def __init__(self, *, result: str = "succeeded", url: str = "https://x/a.mp4") -> None:
        self.result = result
        self.url = url
        self.polled: list[str] = []
        self.created = 0

    async def create_job(self, prompt, params=None):  # pragma: no cover - 不该被调
        self.created += 1
        raise AssertionError("接管任务时不允许重新提交（会重复计费）")

    async def poll_job(self, external_task_id: str) -> VideoJobStatus:
        self.polled.append(external_task_id)
        return VideoJobStatus(
            external_task_id=external_task_id,
            status=self.result,
            progress=1.0 if self.result == "succeeded" else 0.5,
            video_url=self.url if self.result == "succeeded" else None,
            error_message=None if self.result == "succeeded" else "上游失败",
        )


@pytest.fixture()
def fast_poll(monkeypatch):
    """把轮询间隔压到毫秒级，避免测试真的等 5 秒。"""
    monkeypatch.setattr(queue_mod, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(queue_mod, "MAX_POLLS", 50)


@pytest.fixture()
def manager(fast_poll):
    """每个用例一个干净的队列，用完必须**等它停**。

    两层原因：
      1. 线程池的线程是非守护线程，不关的话解释器退出时要 join——表现为
         「343 passed」之后命令行卡住；
      2. 光 shutdown 还不够：`wait=False` 不等正在跑的轮询。返回 `running` 的
         假厂商会让线程一直轮询，而 `fast_poll` 的 monkeypatch 在测试一结束就
         撤销——之后每轮 sleep 变回 5 秒，50 次循环就是 250 秒，退出被拖死。
         所以这里必须 `wait=True`，趁轮询间隔还是毫秒级把线程收干净。
    """
    m = VideoJobManager(max_workers=2)
    yield m
    m.shutdown(wait=True)


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------- attach 本身 ----------


def test_接管只轮询不重新提交(fast_poll, manager):
    seen: list[dict] = []
    manager.on_complete(lambda _jid, info: seen.append(dict(info)))
    provider = FakeProvider()

    assert manager.attach("vjob_1", provider, "ext_1") is True
    assert _wait(lambda: any(s["status"] == "succeeded" for s in seen)), seen

    assert provider.created == 0            # 关键：没有 create_job
    assert provider.polled == ["ext_1"]
    assert seen[-1]["video_url"] == "https://x/a.mp4"


def test_接管是幂等的(fast_poll, manager):
    provider = FakeProvider(result="running")
    assert manager.attach("vjob_1", provider, "ext_1") is True
    assert manager.attach("vjob_1", provider, "ext_1") is False
    assert manager.is_tracked("vjob_1") is True


def test_没有任务号不接管(fast_poll, manager):
    assert manager.attach("vjob_1", FakeProvider(), "") is False


# ---------- 巡检：从数据库把孤儿捞出来 ----------


def _job(store, *, status: str = "generating", external: str | None = "ext_1", provider: str = "fake"):
    p = store.create_project(title="测", adaptation_type="short_drama", language="zh", raw_text="x")
    j = store.create_video_job(
        project_id=p.id, shot_id="shot_scene_001_000", provider=provider,
        model="fake-model", prompt="a shot",
    )
    store.update_video_job(j.id, status=status, external_task_id=external)
    return j


def test_巡检接管孤儿任务(store, fast_poll, manager):
    job = _job(store)
    seen: list[dict] = []
    manager.on_complete(lambda _jid, info: seen.append(dict(info)))

    counts = resume_orphans(
        store=store, manager=manager, resolve_provider=lambda _name: FakeProvider(),
    )
    assert counts["attached"] == 1
    assert manager.is_tracked(job.id)
    assert _wait(lambda: any(s["status"] == "succeeded" for s in seen)), seen


def test_巡检跳过已在盯的任务(store, fast_poll, manager):
    job = _job(store)
    provider = FakeProvider(result="running")
    manager.attach(job.id, provider, "ext_1")

    counts = resume_orphans(store=store, manager=manager, resolve_provider=lambda _n: FakeProvider())
    assert counts["attached"] == 0 and counts["already"] == 1


def test_没有任务号的孤儿判失败并说明(store, fast_poll, manager):
    """进程死在「提交」与「拿到任务号」之间——服务商那边没有这条任务，无从接管。"""
    job = _job(store, external=None)

    counts = resume_orphans(store=store, manager=manager, resolve_provider=lambda _n: FakeProvider())
    assert counts["dropped"] == 1
    row = store.get_video_job(job.id)
    assert row.status == "failed"
    assert "没有服务商任务号" in (row.error_message or "")


def test_服务商配置丢了也判失败(store, fast_poll, manager):
    job = _job(store, provider="已被删掉的厂商")

    counts = resume_orphans(store=store, manager=manager, resolve_provider=lambda _n: None)
    assert counts["dropped"] == 1
    row = store.get_video_job(job.id)
    assert row.status == "failed"
    assert "无法接管" in (row.error_message or "")


def test_已终态的任务不会被巡检捡起来(store, fast_poll, manager):
    _job(store, status="succeeded")

    counts = resume_orphans(store=store, manager=manager, resolve_provider=lambda _n: FakeProvider())
    assert counts == {"attached": 0, "already": 0, "dropped": 0}
    assert store.list_unfinished_video_jobs() == []


class FlakyThenOkProvider(FakeProvider):
    """前 N 次查询抛网络异常，之后正常成功——果园v3 冤案现场复刻：
    一次瞬断绝不能把厂商侧活着的任务判死。"""

    def __init__(self, *, fail_times: int = 3, **kw) -> None:
        super().__init__(**kw)
        self.fail_times = fail_times
        self._fails = 0

    async def create_job(self, prompt, params=None):
        return VideoJobResponse(external_task_id="ext_flaky", status="queued")

    async def poll_job(self, external_task_id: str) -> VideoJobStatus:
        if self._fails < self.fail_times:
            self._fails += 1
            raise RuntimeError("")  # 真实案例里 str(e) 是空的，连消息都没有
        return await FakeProvider.poll_job(self, external_task_id)


def test_transient_poll_errors_do_not_fail_job(manager):
    """查询异常容忍：瞬断重试到成功，任务状态终态是 succeeded 而非 failed。"""
    seen: list[dict] = []
    manager.on_complete(lambda job_id, info: seen.append(dict(info)))
    p = FlakyThenOkProvider(fail_times=3)
    manager.submit("vjob_flaky", p, "prompt")
    deadline = time.time() + 5
    while time.time() < deadline:
        if any(i["status"] in ("succeeded", "failed") for i in seen):
            break
        time.sleep(0.02)
    finals = [i for i in seen if i["status"] in ("succeeded", "failed")]
    assert finals and finals[-1]["status"] == "succeeded", f"瞬断被误判成死：{finals}"
    assert finals[-1]["video_url"] == p.url
