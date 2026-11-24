"""Location state images remain distinct and resolve per scene/shot, without model calls."""

import pytest

from app.domain import Character, Location, Scene, Script, Shot, Source, StyleGuide
from app.media.location_states import all_states, base_id
from app.media.looks import register_generated_images
from app.media.refs import apply_reference_images, build_reference_assets


def fixture():
    script = Script(
        title="故事", logline="一句话", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[Character(id="char_a", name="阿青")],
        locations=[Location(id="loc_room", name="房间")],
        scenes=[Scene(id="scene_001", title="室内", chapter_refs=["ch_001"],
                      location_id="loc_room", characters=["char_a"], purpose="推进", conflict="冲突")],
    )
    shots = [Shot(id=f"shot_scene_001_{n:03d}", scene_id="scene_001", order=n) for n in range(2)]
    return script, shots


def test_location_regeneration_keeps_base_and_explicit_shot_choice():
    script, shots = fixture()
    old = {"reference_images": {"loc_room": "day.png"}}
    patch = register_generated_images(old, script, {"loc_room": "night.png"})
    style = {**old, **patch}
    assert style["reference_images"]["loc_room"] == "day.png"
    states = all_states(style, script)
    night = next(s for s in states.values() if s["image"] == "night.png")
    style["shot_location_state_ids"] = {shots[1].id: night["id"]}
    apply_reference_images(shots, style["reference_images"], script, style=style)
    assert [s.reference_images for s in shots] == [["day.png"], ["night.png"]]


def test_wrong_location_state_fails_closed():
    script, shots = fixture()
    style = {"reference_images": {"loc_room": "day.png"},
             "shot_location_state_ids": {shots[0].id: "missing"}}
    with pytest.raises(ValueError, match="状态不明确"):
        apply_reference_images(shots, style["reference_images"], script, style=style)


def test_generate_only_selected_location_state():
    class Image:
        available = True
        prompts = []

        def generate(self, prompt, *, negative_prompt=""):
            self.prompts.append(prompt)
            return "state.png"

    class Vision:
        available = False

    script, _ = fixture()
    state = {"location_id": "loc_room", "name": "停电", "description": "房间全黑，仅有应急灯"}
    image = Image()
    refs = build_reference_assets(
        script, StyleGuide(location_states={"state_dark": state}),
        image_client=image, vision_client=Vision(), entities=["loc_room"],
        location_state_ids=["state_dark"], regenerate=True,
    )
    assert refs == {"state_dark": "state.png"}
    assert len(image.prompts) == 1 and "应急灯" in image.prompts[0]

    # Ordinary location generation must not silently generate every draft state.
    image.prompts.clear()
    ordinary = build_reference_assets(
        script, StyleGuide(location_states={"state_dark": state}),
        image_client=image, vision_client=Vision(), entities=["loc_room"],
    )
    assert ordinary == {"loc_room": "state.png"}
    assert len(image.prompts) == 1 and "应急灯" not in image.prompts[0]
