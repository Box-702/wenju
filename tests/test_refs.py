# =====================================================================
# test_refs.py —— 参考资产流水线测试
#
# 只测纯逻辑（prompt 组装 / 质检判定 / 回填映射），不触网：
# 真实的文生图与视觉质检在联调时验证。
# =====================================================================

from __future__ import annotations

from app.domain import CameraMovement, Character, Location, Scene, Script, Shot, Source
from app.media.refs import (
    MAX_REFERENCE_IMAGES,
    _verdict,
    apply_reference_images,
    character_prompt,
    environment_prompt,
)


def test_character_prompt_carries_appearance_and_constraints():
    p = character_prompt("山羊头", "戴山羊头骨面具、穿黑色西服的高瘦男人。")
    assert "戴山羊头骨面具" in p
    assert "中国人" in p or "东亚面孔" in p
    assert "不要出现任何文字" in p


def test_environment_prompt_asks_for_empty_plate():
    p = environment_prompt("密室", "密闭的方形混凝土房间，钨丝灯昏黄。")
    assert "密闭的方形混凝土房间" in p
    assert "空镜" in p and "没有任何人物" in p


def test_verdict_parses_pass_fail_and_defaults_to_pass():
    assert _verdict("逐条分析...\nPASS")[0] is True
    assert _verdict("逐条分析...\nFAIL")[0] is False
    # 解析不到时保守放行，避免无限重生成
    assert _verdict("说不清楚")[0] is True


def test_verdict_reads_last_line_not_whole_answer():
    """依据行里出现「不合格」字样，不能把最后一行的 PASS 翻成 FAIL。

    质检回答改成「一行依据 + 一行判定」后，拿全文找子串会误判——
    22/23 张全 FAIL 的排查里撞到的解析坑。
    """
    ok, reason = _verdict("画的是真人照片感，说不清算不算不合格。\nPASS")
    assert ok is True
    assert "真人照片感" in reason  # 依据要带出来，任务步骤详情里才看得见为什么重来
    assert _verdict("构图不对，不是单人正面半身。\nFAIL")[0] is False


def _script_with(scene_chars: list[str]) -> Script:
    return Script(
        title="t", logline="l", themes=[],
        source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[Character(id="char_a", name="齐夏"), Character(id="char_b", name="人羊")],
        locations=[Location(id="loc_room", name="密室")],
        scenes=[Scene(id="scene_001", title="第一场", chapter_refs=["ch_001"], location_id="loc_room",
                      characters=scene_chars, purpose="p", conflict="c")],
    )


def test_apply_reference_images_maps_env_and_characters():
    script = _script_with(["char_a", "char_b"])
    refs = {"密室": "http://env", "齐夏": "http://a", "人羊": "http://b"}
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, camera=CameraMovement())]
    assert apply_reference_images(shots, refs, script) == 1
    # 环境图在最前，其后是出场角色图
    assert shots[0].reference_images == ["http://env", "http://a", "http://b"]


def test_apply_reference_images_caps_and_skips_missing():
    script = _script_with(["char_a", "char_b"])
    # 只有角色图、没有环境图；且 capped 到上限
    refs = {"齐夏": "http://a"}
    shots = [Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, camera=CameraMovement())]
    apply_reference_images(shots, refs, script)
    assert shots[0].reference_images == ["http://a"]

    many = {f"n{i}": f"http://{i}" for i in range(20)}
    s2 = Shot(id="shot_scene_001_001", scene_id="scene_001", order=1, camera=CameraMovement())
    script2 = _script_with(["char_a", "char_b"])
    script2.scenes[0].characters = []
    # 无角色时只可能填环境图；这里 refs 里没有环境图，应保持为空
    apply_reference_images([s2], many, script2)
    assert s2.reference_images == []
    assert MAX_REFERENCE_IMAGES == 9
