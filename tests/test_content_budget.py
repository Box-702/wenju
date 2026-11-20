# =====================================================================
# test_content_budget.py —— 内容比例预算与按时长拆集（2026-11-20）
#
# 《替身v2》复盘第二个病根：固定 120 秒预算不看内容量，等于逼导演删事件。
# 10 场 52 个节拍被闸门「删掉 16 个镜头」，苏晴的核心台词整句消失——
# 而被删的恰恰是最需要独立镜头的事件（一个镜塞两个事件 = 人物挤出画外
# = 漂移之源）。即梦对同一篇原文的路线是反的：5 集 × 60–95 秒，
# 事件不删、按集摊开。
#
# 修法：预算 = max(类型预算, 场数 × 每场保底 24 秒)；超一集时长的章
# 按时长贪心装箱拆成多集（assign_episodes）。
# =====================================================================

from __future__ import annotations

from app.crew.director import assign_episodes
from app.domain import (
    Character,
    Location,
    Scene,
    SceneBreakdown,
    Script,
    ScriptBeat,
    Shot,
    Source,
)
from app.video.rhythm import (
    MIN_SCENE_BUDGET_SEC,
    content_duration_budget,
    duration_budget,
)


def test_预算按场数保底_小内容不变():
    # 少场数：维持旧的 120 秒（行为与改造前一致）
    assert content_duration_budget("short_drama", 0) == duration_budget("short_drama")
    assert content_duration_budget("short_drama", 3) == 120.0


def test_预算按场数保底_大内容抬高():
    # 《替身v2》实况：10 场 → 保底 240 秒，不再逼导演删 16 镜
    assert content_duration_budget("short_drama", 10) == 10 * MIN_SCENE_BUDGET_SEC
    # 上限仍受类型预算兜底方向约束：film 的 6000 秒不会被 10 场抬高
    assert content_duration_budget("film", 10) == duration_budget("film")


def _shot(order: int, dur: float, scene_id: str) -> Shot:
    return Shot(id=f"shot_{scene_id}_{order:03d}", scene_id=scene_id,
                order=order, duration_sec=dur, action="他站起来，走到桌前")


def _script(scene_ids: list[str], chapter: str = "ch_001") -> Script:
    return Script(
        title="t", version="1.0", language="zh-CN", logline="甲在房间里。",
        source=Source(chapter_count=1, chapter_ids=[chapter]),
        characters=[Character(id="char_001", name="甲")],
        locations=[Location(id="loc_001", name="房间")],
        scenes=[Scene(id=sid, title=sid, chapter_refs=[chapter],
                      location_id="loc_001", characters=["char_001"],
                      purpose="p", conflict="c",
                      beats=[ScriptBeat(id="beat_001", type="action", text="x")])
                for sid in scene_ids],
    )


def test_单章超一集时自动拆集():
    # 10 场 × 每场 24 秒 = 240 秒 ≈ 两集（EPISODE_TARGET_SEC=120）
    scene_ids = [f"scene_{i:03d}" for i in range(1, 11)]
    bds = []
    order = 0
    for sid in scene_ids:
        shots = [_shot(order, 4.0, sid), _shot(order + 1, 4.0, sid),
                 _shot(order + 2, 4.0, sid), _shot(order + 3, 4.0, sid),
                 _shot(order + 4, 4.0, sid)]
        order += 5
        bds.append(SceneBreakdown(scene_id=sid, shots=shots))
    assign_episodes(bds, _script(scene_ids))
    eps = {s.episode_id for bd in bds for s in bd.shots}
    assert len(eps) == 2, eps                       # 240 秒拆成两集
    idxs = {s.episode_index for bd in bds for s in bd.shots}
    assert idxs == {0, 1}
    # 场次顺序不交叉：后一集的场全部排在后面
    first_ep_scenes = [bd.scene_id for bd in bds
                       if bd.shots[0].episode_index == 0]
    second_ep_scenes = [bd.scene_id for bd in bds
                        if bd.shots[0].episode_index == 1]
    assert first_ep_scenes + second_ep_scenes == scene_ids


def test_短章不拆集_行为与旧版一致():
    scene_ids = ["scene_001", "scene_002"]
    bds = [SceneBreakdown(scene_id=sid, shots=[_shot(i, 4.0, sid)])
           for i, sid in enumerate(scene_ids)]
    assign_episodes(bds, _script(scene_ids))
    eps = {s.episode_id for bd in bds for s in bd.shots}
    assert eps == {"ch_001"}                        # 没有 #n 后缀


def test_多章仍按章分集():
    ids_a = ["scene_001", "scene_002"]
    ids_b = ["scene_003"]
    script = _script(ids_a + ids_b, chapter="ch_001")
    script.scenes[2].chapter_refs = ["ch_002"]
    script.source = Source(chapter_count=2, chapter_ids=["ch_001", "ch_002"])
    bds = [SceneBreakdown(scene_id=sid, shots=[_shot(i, 4.0, sid)])
           for i, sid in enumerate(ids_a + ids_b)]
    assign_episodes(bds, script)
    ep_of = {bd.scene_id: bd.shots[0].episode_id for bd in bds}
    assert ep_of == {"scene_001": "ch_001", "scene_002": "ch_001",
                     "scene_003": "ch_002"}
