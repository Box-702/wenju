"""D-80 look selection and legacy image compatibility; no paid model calls."""

import pytest

from app.domain import Character, Location, Scene, Script, Shot, Source
from app.media.looks import all_looks, base_id, register_generated_images
from app.media.refs import apply_reference_images


def _fixture():
    script = Script(
        title="故事", logline="一句话", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[Character(id="char_a", name="阿青")],
        locations=[Location(id="loc_room", name="房间")],
        scenes=[Scene(id="scene_001", title="室内", chapter_refs=["ch_001"],
                      location_id="loc_room", characters=["char_a"], purpose="推进", conflict="冲突")],
    )
    shots = [Shot(id=f"shot_scene_001_{n:03d}", scene_id="scene_001", order=n)
             for n in range(3)]
    return script, shots


def test_old_image_is_base_and_regeneration_preserves_it():
    script, shots = _fixture()
    old = {"reference_images": {"阿青": "old.png", "loc_room": "room.png"},
           "character_appearances": {"char_a": "蓝衣"}}
    patch = register_generated_images(old, script, {"char_a": "new.png"})
    assert patch["reference_images"]["char_a"] == "old.png"
    style = {**old, **patch}
    looks = all_looks(style, script)
    assert looks[base_id("char_a")]["image"] == "old.png"
    assert any(v["image"] == "new.png" for v in looks.values())
    apply_reference_images([shots[0]], style["reference_images"], script,
                           style=style, shot_plan=shots)
    assert shots[0].reference_images == ["room.png", "old.png"]


def test_shot_override_and_first_eligible_shot():
    script, shots = _fixture()
    style = {"reference_images": {"char_a": "daily.png"},
             "character_looks": {"look_injured": {
                 "character_id": "char_a", "name": "受伤", "description": "绷带",
                 "image": "injured.png", "available_from_shot_id": shots[1].id}},
             "shot_look_ids": {shots[1].id: {"char_a": "look_injured"}}}
    apply_reference_images(shots, style["reference_images"], script,
                           style=style, shot_plan=shots)
    assert [s.reference_images for s in shots] == [["daily.png"], ["injured.png"], ["daily.png"]]
    style["shot_look_ids"] = {shots[0].id: {"char_a": "look_injured"}}
    with pytest.raises(ValueError, match="只能从镜头"):
        apply_reference_images([shots[0]], style["reference_images"], script,
                               style=style, shot_plan=shots)


def test_no_base_never_guesses_newest_look():
    script, shots = _fixture()
    style = {"character_looks": {"look_formal": {
        "character_id": "char_a", "name": "礼服", "description": "黑色礼服",
        "image": "formal.png", "available_from_shot_id": shots[0].id}}}
    with pytest.raises(ValueError, match="造型不明确"):
        apply_reference_images([shots[0]], {}, script, style=style, shot_plan=shots)
