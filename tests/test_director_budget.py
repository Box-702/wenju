# =====================================================================
# test_director_budget.py —— 片长闸门不能砍掉台词、不能删空整场
#
# 《替身》试点出片后用户看片提了两条：
#   1. 「第一镜话说不完」——台词被片尾截断；
#   2. 「故事不连贯」——整场戏在分镜里消失了。
# 两条都不是提示词没写（导演技能第 6c 条与「每场至少 2 镜」都写了），
# 而是**闸门在提示词之后把规则推翻了**：
#   - 「单镜封顶 6 秒」不看有没有台词 → 25 字需要 6.5 秒，被砍到 6 秒；
#   - 「无台词优先删」把整场《偷偷的甜》6 个无对白动作镜连根拔掉。
# 这里把新规则钉死：闸门改完之后，这两条不能再犯。
# =====================================================================

from __future__ import annotations

from app.crew.director import (
    CAP_SHOT_SEC,
    MIN_SHOTS_PER_SCENE,
    dialogue_need_sec,
    enforce_duration_budget,
    scene_content_weights,
)
from app.crew.dp import stretch_for_dialogue
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


def _beat(beat_id: str, *, line: str = "", text: str = "") -> ScriptBeat:
    if line:
        return ScriptBeat(id=beat_id, type="dialogue", speaker="char_a", line=line)
    return ScriptBeat(id=beat_id, type="action", text=text)


def _shot(shot_id: str, order: int, dur: float, *, scene: str = "scene_001",
          ref: str = "", function: str = "action", group: str = "") -> Shot:
    return Shot(
        id=shot_id, scene_id=scene, order=order, duration_sec=dur,
        shot_type="medium", subject="甲", action="甲推开门走进来；乙抬头看他；甲走到桌前站住",
        function=function, dialogue_ref=ref, group_id=group,
    )


def _script(*scenes: tuple[str, list[ScriptBeat]]) -> Script:
    sc_objs = [
        Scene(id=sid, title=sid, chapter_refs=["ch_001"], location_id="loc_a",
              characters=["char_a"], purpose="p", conflict="c", beats=beats)
        for sid, beats in scenes
    ]
    return Script(
        title="测试", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        logline="测", characters=[Character(id="char_a", name="甲")],
        locations=[Location(id="loc_a", name="屋")], scenes=sc_objs,
    )


def test_口播时长口径() -> None:
    assert dialogue_need_sec([]) == 0.0
    # 统一口径（app.video.rhythm）：中文 3.8 字/秒 + 1s 表演余量。
    # 实测《替身》第一镜就是这句——旧口径算 6.45s 被砍到 6s 截断，
    # 新口径 7.2s，闸门必须保住它。
    need = dialogue_need_sec([_beat("beat_001", line="你叫β。你的工作是替我上班——每天和公司的AI对话，训练它。")])
    assert abs(need - 7.2) < 0.01


def test_闸门不把台词镜砍到念不完() -> None:
    """8 秒的台词镜（25 字，需 6.45 秒）：封顶 6 秒也够念——但 41 字就不够。"""
    line = "以后都会这样。对了，那个……工作的事，我自己能搞定，我想多陪陪你和女儿，β我送去克隆人安葬中心了。"
    beats = [_beat("beat_001", line=line)]
    shots = [_shot("shot_scene_001_000", 0, 8.0, ref="beat_001")]
    bd = SceneBreakdown(scene_id="scene_001", shots=shots)
    script = _script(("scene_001", beats))

    from app.crew.director import dialogue_map_for

    gated, notes = enforce_duration_budget(
        [bd], 120.0,
        shots_dialogue=dialogue_map_for(script, [bd]),
        scene_weights=scene_content_weights(script),
    )
    kept = gated[0].shots[0]
    need = dialogue_need_sec([b for b in beats if b.type == "dialogue"])
    assert kept.duration_sec >= need, f"台词镜被砍到 {kept.duration_sec}s，念不完（需 {need:.1f}s）"
    assert any("念得完" in n or "保留超过" in n for n in notes)


def test_闸门不删空整场() -> None:
    """《偷偷的甜》式的一场：全是无对白动作镜，也不能被整场删光。"""
    silent_beats = [_beat(f"beat_{i:03d}", text="β偷吃了一口巧克力") for i in range(1, 4)]
    talk_beats = [_beat("beat_009", line="你干什么？！放我出去！")]
    shots = [
        _shot("shot_scene_001_000", 0, 8.0, scene="scene_001"),
        _shot("shot_scene_001_001", 1, 8.0, scene="scene_001"),
        _shot("shot_scene_001_002", 2, 8.0, scene="scene_001"),
        _shot("shot_scene_002_000", 3, 8.0, scene="scene_002", ref="beat_009"),
        _shot("shot_scene_002_001", 4, 8.0, scene="scene_002"),
        _shot("shot_scene_002_002", 5, 8.0, scene="scene_002"),
    ]
    bds = [
        SceneBreakdown(scene_id="scene_001", shots=[s for s in shots if s.scene_id == "scene_001"]),
        SceneBreakdown(scene_id="scene_002", shots=[s for s in shots if s.scene_id == "scene_002"]),
    ]
    script = _script(("scene_001", silent_beats), ("scene_002", talk_beats))

    from app.crew.director import dialogue_map_for

    gated, notes = enforce_duration_budget(
        bds, 20.0,   # 预算远小于内容（48 秒）→ 必然要删镜
        shots_dialogue=dialogue_map_for(script, bds),
        scene_weights=scene_content_weights(script),
    )
    per_scene = {bd.scene_id: len(bd.shots) for bd in gated}
    assert per_scene.get("scene_001", 0) >= MIN_SHOTS_PER_SCENE, \
        f"整场被删空了：{per_scene}"
    assert per_scene.get("scene_002", 0) >= 1, f"另一场被删空了：{per_scene}"


def test_闸门按内容量给场分预算() -> None:
    """内容多的场（节拍多）该拿到更多时长，不能被平均砍平。"""
    beats_a = [_beat(f"beat_{i:03d}", text="甲走过去") for i in range(1, 10)]   # 9 节拍
    beats_b = [_beat(f"beat_{i:03d}", text="乙跑过来") for i in range(1, 3)]    # 2 节拍
    shots = [
        _shot("shot_scene_001_000", 0, 10.0, scene="scene_001"),
        _shot("shot_scene_001_001", 1, 10.0, scene="scene_001"),
        _shot("shot_scene_001_002", 2, 10.0, scene="scene_001"),
        _shot("shot_scene_002_000", 3, 10.0, scene="scene_002"),
        _shot("shot_scene_002_001", 4, 10.0, scene="scene_002"),
    ]
    bds = [
        SceneBreakdown(scene_id="scene_001", shots=[s for s in shots if s.scene_id == "scene_001"]),
        SceneBreakdown(scene_id="scene_002", shots=[s for s in shots if s.scene_id == "scene_002"]),
    ]
    script = _script(("scene_001", beats_a), ("scene_002", beats_b))

    from app.crew.director import dialogue_map_for

    gated, _ = enforce_duration_budget(
        bds, 24.0,
        shots_dialogue=dialogue_map_for(script, bds),
        scene_weights=scene_content_weights(script),
    )
    dur = {bd.scene_id: sum(s.duration_sec for s in bd.shots) for bd in gated}
    assert dur.get("scene_001", 0) > dur.get("scene_002", 0), \
        f"内容量多的场没拿到更多时长：{dur}"


def test_dp层把念不完的镜头拉长() -> None:
    """花钱前最后一道：6 秒的镜念 41 字 → 抬到念得完。"""
    line = "以后都会这样。对了，那个……工作的事，我自己能搞定，我想多陪陪你和女儿，β我送去克隆人安葬中心了。"
    beats = [_beat("beat_001", line=line)]
    script = _script(("scene_001", beats))
    shots = [_shot("shot_scene_001_000", 0, 6.0, ref="beat_001")]
    notes = stretch_for_dialogue(shots, script)
    need = dialogue_need_sec([b for b in beats if b.type == "dialogue"])
    assert shots[0].duration_sec >= need
    assert notes and "shot_scene_001_000" in notes[0]


def test_dp层拉长不顶破组15秒() -> None:
    """组内机位共用一条 15 秒预算：拉长不能让组超上限。"""
    line = "以后都会这样。对了，那个……工作的事，我自己能搞定，我想多陪陪你和女儿，β我送去克隆人安葬中心了。"
    beats = [_beat("beat_001", line=line)]
    script = _script(("scene_001", beats))
    shots = [
        _shot("shot_scene_001_000", 0, 6.0, ref="beat_001", group="g1"),
        _shot("shot_scene_001_001", 1, 8.0, group="g1"),
    ]
    stretch_for_dialogue(shots, script)
    total = sum(s.duration_sec for s in shots)
    assert total <= 15.0 + 0.01, f"组总时长 {total} 顶破了 15 秒硬边界"


def test_封顶常量仍是节奏基线而不是硬上限() -> None:
    """CAP_SHOT_SEC 是节奏基线：台词镜允许超过它（2026-11-19 实测教训）。"""
    assert CAP_SHOT_SEC == 6.0
    assert MIN_SHOTS_PER_SCENE >= 1


def test_组删除不绕过保底() -> None:
    """《囚禁》式场景：2 镜恰是一个组（2 机位），一刀 = 整场删空。

    2026-11-19 重拆实测踩中：2c 的「shot_count > 1」检查在**删之前**看是
    2 镜（通过），组一刀下去 -2，整场归零。保底必须按「删完之后剩几镜」算。
    """
    beats = [_beat("beat_001", text="β站在舱外看着舱内的林远")]
    shots = [
        # 一个组 2 机位（同 group_id），一场就这俩镜
        _shot("shot_scene_001_000", 0, 6.0, group="g_trap"),
        _shot("shot_scene_001_001", 1, 6.0, group="g_trap"),
    ]
    bd = SceneBreakdown(scene_id="scene_001", shots=shots)
    script = _script(("scene_001", beats))

    from app.crew.director import dialogue_map_for

    gated, notes = enforce_duration_budget(
        [bd], 5.0,   # 预算极小：怎么删都超
        shots_dialogue=dialogue_map_for(script, [bd]),
        scene_weights=scene_content_weights(script),
    )
    assert len(gated[0].shots) >= 1, "组一刀两镜把整场删空了"
    assert sum(s.duration_sec for s in gated[0].shots) > 5.0 or notes, \
        "保不住保底时必须在 notes 里说明"
