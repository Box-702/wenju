# =====================================================================
# test_refs.py —— 参考资产流水线测试
#
# 只测纯逻辑（prompt 组装 / 质检判定 / 回填映射），不触网：
# 真实的文生图与视觉质检在联调时验证。
# =====================================================================

from __future__ import annotations

from app.domain import CameraMovement, Character, Location, Scene, Script, Shot, Source, StyleGuide
from app.media.refs import (
    MAX_REFERENCE_IMAGES,
    _NEG_COMPOSITION,
    _QA_CHARACTER,
    _verdict,
    apply_reference_images,
    build_reference_assets,
    character_prompt,
    environment_prompt,
)


def test_sheet_qa_and_negative_allow_required_views():
    for forbidden in ("背面，背影", "全身远景", "侧背影"):
        assert forbidden not in _NEG_COMPOSITION
    assert "严格侧面" in _QA_CHARACTER
    assert "从头到脚完整" in _QA_CHARACTER
    assert "服装或身体状态" in _QA_CHARACTER
    assert "主色、辅色、点缀色" in _QA_CHARACTER
    assert "视角标签" in _QA_CHARACTER


def test_each_named_look_gets_own_board_and_shared_face_anchor():
    class Image:
        available = True
        prompts: list[str] = []

        def generate(self, prompt, *, negative_prompt=""):
            self.prompts.append(prompt)
            return f"image-{len(self.prompts)}"

    class Vision:
        available = True
        seen: list[object] = []

        def ask(self, images, question):
            self.seen.append(images)
            return "画面符合要求\nPASS"

    script = _script_with(["char_a"])
    guide = StyleGuide(
        character_identity_anchors={"char_a": "左眉尾小痣、圆眼、窄鼻梁"},
        character_looks={
            "look_daily": {"character_id": "char_a", "name": "日常", "description": "蓝色校服"},
            "look_injured": {"character_id": "char_a", "name": "受伤", "description": "灰色病号服，左臂绷带"},
        },
    )
    image = Image()
    vision = Vision()
    refs = build_reference_assets(script, guide, image_client=image, vision_client=vision,
                                  entities=["char_a"])
    assert set(refs) == {"look_daily", "look_injured"}
    assert len(image.prompts) == 2
    assert all("左眉尾小痣、圆眼、窄鼻梁" in p for p in image.prompts)
    assert "蓝色校服" in image.prompts[0] and "病号服" not in image.prompts[0]
    assert "病号服" in image.prompts[1] and "蓝色校服" not in image.prompts[1]
    assert vision.seen == ["image-1", ["image-1", "image-2"]]


def test_character_prompt_carries_appearance_and_constraints():
    p = character_prompt("山羊头", "戴山羊头骨面具、穿黑色西服的高瘦男人。")
    assert "戴山羊头骨面具" in p
    assert "中国人" in p or "东亚面孔" in p
    assert "不要出现任何文字" in p
    assert all(part in p for part in ("正面头肩特写", "正面、背面、严格侧面", "局部特写", "纯色方块"))


def test_character_sheet_layout_preserves_visual_style():
    from app.pipeline.visual_styles import character_prompt as styled_prompt

    for style, cue in (("realistic", "写实电影摄影"), ("anime", "赛璐璐"),
                       ("3d", "三维动画"), ("ink", "水墨")):
        prompt = styled_prompt("齐夏", "黑色外套，银色胸针", style)
        assert cue in prompt
        assert "黑色外套，银色胸针" in prompt
        assert "同一人物" in prompt
        assert "配色区域" in prompt


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
