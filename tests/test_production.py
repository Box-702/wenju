# =====================================================================
# test_production.py —— 生产工作流（小说 → 提示词）测试
#
# 锁四条：
#   1. 五个阶段按序跑完，产物全部落库（剧本版本 / 拆解 / 风格 / 资产图 / 提示词版本）；
#   2. 重跑幂等：已有产物直接复用，不再调生成函数（不重复花钱）；
#   3. 提示词产出的是「待审」草稿——不能绕过人工批准门禁；
#   4. 进度回调按阶段触发（任务面板靠它实时上报）。
# =====================================================================

from __future__ import annotations

import pytest

from app.agent import production as prod_mod
from app.agent.production import STAGES, run_production


@pytest.fixture()
def project(store):
    p = store.create_project(
        title="测试剧", adaptation_type="short_drama", language="zh", raw_text="原文内容。",
    )
    return store.get_project(p.id)


@pytest.fixture()
def patched_llm(monkeypatch):
    """五个阶段的重函数全部替换为假实现，记录调用次数（验证幂等）。"""
    calls = {"generate": 0, "director": 0, "art": 0, "refs": 0, "dp": 0}

    def fake_generate(llm, settings, **kw):
        calls["generate"] += 1
        from app.domain import (
            Character, Location, Scene, Script, Source,
        )

        script = Script(
            title="测试剧", logline="梗概", themes=["悬疑"],
            source=Source(chapter_count=1, chapter_ids=["ch_001"]),
            characters=[Character(id="char_a", name="齐夏")],
            locations=[Location(id="loc_room", name="密室")],
            scenes=[Scene(id="scene_001", title="第一场", chapter_refs=["ch_001"],
                          location_id="loc_room", characters=["char_a"],
                          purpose="建立冲突", conflict="对峙")],
        )
        return script, {"mode": "single"}

    class _Result:
        def __init__(self, data, summary):
            self.success = True
            self.data = data
            self.summary = summary

    def fake_director(llm, script, **kw):
        calls["director"] += 1
        from app.domain import CameraMovement, SceneBreakdown, Shot

        bd = SceneBreakdown(
            scene_id="scene_001", title="第一场", purpose="建立冲突",
            shots=[Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                        camera=CameraMovement(type="static"))],
        )
        return _Result([bd], "1 场 1 镜")

    def fake_art(llm, script, **kw):
        calls["art"] += 1
        from app.domain import StyleGuide

        return _Result(
            StyleGuide(
                character_appearances={"char_a": "二十岁男性，短发"},
                environment_descriptions={"loc_room": "昏暗密室"},
            ),
            "风格指南",
        )

    def fake_refs(script, guide, on_progress=None):
        calls["refs"] += 1
        return {"char_a": "http://img/char_a", "loc_room": "http://img/loc_room"}

    def fake_dp(llm, script, **kw):
        calls["dp"] += 1
        from app.domain import CameraMovement, Shot

        shot = Shot(id="shot_scene_001_000", scene_id="scene_001", order=0,
                    camera=CameraMovement(type="static"))
        shot.video_prompt = "昏暗密室@密室(loc_room)。人物：齐夏@齐夏(char_a)。齐夏推门。"
        return _Result([shot], "1 镜提示词")

    monkeypatch.setattr(prod_mod, "run_generation", fake_generate, raising=False)
    # production.py 内部是函数内 import，patch 源头模块
    import app.pipeline.generation as gen_mod
    monkeypatch.setattr(gen_mod, "generate_script", fake_generate)
    import app.crew.director as director_mod
    monkeypatch.setattr(director_mod, "run_director", fake_director)
    import app.crew.art_director as art_mod
    monkeypatch.setattr(art_mod, "run_art_director", fake_art)
    import app.media.refs as refs_mod
    monkeypatch.setattr(refs_mod, "build_reference_assets", fake_refs)
    import app.crew.dp as dp_mod
    monkeypatch.setattr(dp_mod, "run_dp", fake_dp)

    return calls


def test_五阶段按序跑完并落库(store, project, patched_llm):
    result = run_production(store, None, None, project.id)

    assert patched_llm == {"generate": 1, "director": 1, "art": 1, "refs": 1, "dp": 1}
    # 剧本版本落库
    version = store.latest_version(project)
    assert version is not None
    assert store.get_version_breakdown(version.id), "拆解应写入 script_version"
    # 提示词版本落库且是最新 video version
    vvs = store.list_video_versions(project.id)
    assert vvs
    latest = sorted(vvs, key=lambda v: v.created_at)[-1]
    assert latest.shots[0]["prompt_status"] == "needs_review"
    # 提示词带资产编号
    assert "@齐夏(char_a)" in latest.shots[0]["video_prompt"]
    assert result["prompt_version_id"] == latest.id
    assert len(result["notes"]) == 5


def test_重跑幂等不重复花钱(store, project, patched_llm):
    run_production(store, None, None, project.id)
    run_production(store, None, None, project.id)

    # 五个阶段全部命中「复用」分支，生成函数一次都不多调。
    assert patched_llm == {"generate": 1, "director": 1, "art": 1, "refs": 1, "dp": 1}
    # 但提示词阶段仍会产出新版本吗？——不：dp 也只调一次，
    # 第二轮 prompts_node 复用最新待审版本？当前实现每次都新建版本。
    # 这里锁定的行为是「不重复调模型」，版本策略见 prompts_node 注释。
    assert store.list_video_versions(project.id)


def test_进度回调按阶段触发(store, project, patched_llm):
    seen: list[str] = []
    run_production(store, None, None, project.id,
                   on_progress=lambda stage, msg: seen.append(stage))
    # 一个阶段可能上报多条（开始 + 结果），但首次出现的顺序必须等于阶段顺序。
    first_seen = list(dict.fromkeys(seen))
    assert first_seen == list(STAGES)


def test_项目不存在直接报错(store, patched_llm):
    with pytest.raises(ValueError, match="项目不存在"):
        run_production(store, None, None, "proj_nope")
