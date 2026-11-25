"""Canvas image settings and single-target submission stay bounded and versioned."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import production
from app.domain import Character, Location, Scene, Script, Source
from app.media.image_canvas import image_model_options, output_size


def test_canvas_sizes_and_enabled_models(store):
    store.create_api_provider(kind="image", name="minimax", label="MiniMax", api_key="test",
                              config={"models": [{"model": "MiniMax-H3-Image", "enabled": True},
                                                 {"model": "disabled", "enabled": False}]})
    store.create_api_provider(kind="image", name="seedance", label="Seedream", api_key="",
                              config={"model": "seedream-test"})
    models = image_model_options(store)
    assert [m["model_name"] for m in models] == ["MiniMax-H3-Image"]
    assert output_size("minimax", "16:9", "2K") == "2720x1536"
    assert output_size("seedance", "9:16", "1K") == "768x1360"
    with pytest.raises(ValueError, match="不支持"):
        output_size("minimax", "21:9", "2K")


def test_canvas_generates_one_new_scene_state_without_replacing_base(store, monkeypatch):
    project = store.create_project(title="旧车站", adaptation_type="short_drama",
                                   language="zh-CN", raw_text="站台无人。")
    script = Script(
        title="旧车站", logline="雨夜", source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        characters=[Character(id="char_a", name="旅人")],
        locations=[Location(id="loc_station", name="旧车站")],
        scenes=[Scene(id="scene_001", title="站台", chapter_refs=["ch_001"],
                      location_id="loc_station", characters=["char_a"], purpose="等待", conflict="迟到")],
    )
    store.create_version(project, script, source_type="manual")
    store.merge_video_style(project.id, {
        "reference_images": {"loc_station": "day.png"},
        "environment_descriptions": {"loc_station": "白天阳光照着站台"},
    })
    provider = store.create_api_provider(kind="image", name="minimax", label="MiniMax",
                                         api_key="test", config={"model": "MiniMax-H3-Image"})

    class Image:
        available = True
        calls = []

        def generate(self, prompt, *, size, negative_prompt):
            self.calls.append((prompt, size, negative_prompt))
            return "night.png"

    class Runner:
        result = ""

        def start(self, name, fn, *, project_id):
            self.result = fn(SimpleNamespace(id="task_canvas"))
            return "task_canvas"

        def update_step(self, *_):
            pass

    image = Image()
    runner = Runner()
    monkeypatch.setattr(production.deps, "store", lambda: store)
    monkeypatch.setattr(production.deps, "subagent_runner", lambda: runner)
    monkeypatch.setattr(production.deps, "llm", lambda: SimpleNamespace(
        vision=lambda: SimpleNamespace(available=False)))
    monkeypatch.setattr("app.llm.image.build_image_client", lambda _: image)

    request = production.CanvasImageRequest(
        target_kind="location", target_id="loc_station", prompt="雨夜空站台，灯光映在积水上",
        variant_name="雨夜", provider_id=provider.id, model_name="MiniMax-H3-Image",
        ratio="16:9", quality="1K",
    )
    response = production.generate_canvas_image(project.id, request)
    assert response["count"] == 1
    assert len(image.calls) == 1 and image.calls[0][1] == "1360x768"
    assert "雨夜空站台" in image.calls[0][0]
    assert "白天阳光" not in image.calls[0][0]
    style = store.get_video_style(project.id)
    assert style["reference_images"]["loc_station"] == "day.png"
    states = list(style["location_states"].values())
    assert any(s["name"] == "雨夜" and s["image"] == "night.png"
               and s["description"] == request.prompt for s in states)

    with pytest.raises(HTTPException, match="所选图片模型未配置"):
        production.generate_canvas_image(project.id, request.model_copy(update={"model_name": "unknown"}))
