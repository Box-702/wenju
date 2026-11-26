"""剧本型原文的两处塌陷回归（2026-11-20《替身v2》实测踩中）：

1. 场景规划里用 **id** 指代人物（"char_001"）而不是名字（"林远"）时，
   旧代码只按名字匹配 → 认不出 → 当成「原著没出现过的新人」→ 凭空多出
   假角色（char_001_1，名字就叫 "char_001"）。7 个角色被撑成 12 个，
   下游照着假角色多生 5 张定妆照，提示词里还会冒出叫 char_001 的「角色」。
2. 剧本型原文的场次标题与昼夜全丢：8 场全落成兜底「第 N 场」、time 全 None。
   昼夜直接决定下游视频提示词里的光线。

对应实现：app/pipeline/generation.py（别名索引）+ app/pipeline/screenplay.py（回填）。
"""

from __future__ import annotations

import app.pipeline.generation as gen
from app.pipeline.generation import Bible, ScenePlan
from app.pipeline.screenplay import parse_screenplay_blocks


class _Avail:
    available = True


_RAW = """人物设定
林远，38岁，某科技公司AI训练师。

第一场 · 诞生

内景 · 林远家书房 · 夜
林远递给β一支营养液。

内景 · 公司打印区 · 日
打印机卡纸了。

第二场 · 第一次味道

内景 · 公司聚餐包间 · 夜
黑森林蛋糕端上来。
"""


def _bible() -> Bible:
    return Bible.model_validate({
        "title": "替身",
        "logline": "克隆体取代本体。",
        "characters": [
            {"id": "char_001", "name": "林远"},
            {"id": "char_002", "name": "林远-β"},
        ],
        "locations": [
            {"id": "loc_001", "name": "林远家书房"},
            {"id": "loc_002", "name": "公司打印区"},
            {"id": "loc_003", "name": "公司聚餐包间"},
        ],
    })


def _plan(speaker: str = "char_001") -> ScenePlan:
    return ScenePlan.model_validate({"scenes": [
        {"scene_id": "scene_001", "location_id": "loc_001", "characters": ["char_001"],
         "beats": [{"type": "dialogue", "speaker": speaker, "line": "从今天起你叫β。"}]},
        {"scene_id": "scene_002", "location_id": "loc_002", "characters": ["char_002"],
         "beats": [{"type": "action", "text": "打印机卡纸了。"}]},
        {"scene_id": "scene_003", "location_id": "loc_003", "characters": ["char_002"],
         "beats": [{"type": "action", "text": "蛋糕端上来。"}]},
    ]})


def _run(monkeypatch, plan: ScenePlan, raw: str = _RAW):
    monkeypatch.setattr(gen, "_stage_bible", lambda *a, **k: _bible())
    monkeypatch.setattr(gen, "_stage_scenes", lambda *a, **k: plan)
    return gen.generate_script(
        _Avail(), None, title="替身", raw_text=raw,
        adaptation_type="short_drama", language="zh-CN",
    )


def test_说话人用id指代时不再凭空多出假角色(monkeypatch):
    script, _ = _run(monkeypatch, _plan("char_001"))
    assert [c.id for c in script.characters] == ["char_001", "char_002"]
    assert [c.name for c in script.characters] == ["林远", "林远-β"]
    # 第一场说话的应该是林远本人，不是新冒出来的「char_001_1」
    # （首拍可能是后补的开场卡点，跳过 card 再看台词）
    first_line = next(b for b in script.scenes[0].beats if b.type != "card")
    assert first_line.speaker == "char_001"
    assert script.scenes[0].characters == ["char_001"]


def test_说话人带尾号id也能认回本人(monkeypatch):
    script, _ = _run(monkeypatch, _plan("char_001_1"))
    assert [c.id for c in script.characters] == ["char_001", "char_002"]
    first_line = next(b for b in script.scenes[0].beats if b.type != "card")
    assert first_line.speaker == "char_001"


def test_原著里真没出现过的人仍然补上(monkeypatch):
    """护栏不能改瞎：说话人确实是新人物时，照旧补一个角色。"""
    plan = ScenePlan.model_validate({"scenes": [
        {"scene_id": "scene_001", "location_id": "loc_001", "characters": ["收银员"],
         "beats": [{"type": "dialogue", "speaker": "收银员", "line": "扫好了。"}]},
    ]})
    script, _ = _run(monkeypatch, plan)
    names = [c.name for c in script.characters]
    assert "收银员" in names
    first_line = next(b for b in script.scenes[0].beats if b.type != "card")
    assert first_line.speaker == script.characters[-1].id


def test_剧本型原文回填场次标题与昼夜(monkeypatch):
    script, artifacts = _run(monkeypatch, _plan())
    titles = [(s.title, s.time) for s in script.scenes]
    assert titles == [
        ("诞生", "夜"),          # 第一场的第一个取景块
        ("诞生·打印区", "日"),    # 同一场里的第二个取景块：带地点短名
        ("第一次味道", "夜"),
    ]
    assert artifacts["source_backfill"] == {"titles": 3, "times": 3}


def test_模型写对的标题与昼夜不被覆盖(monkeypatch):
    plan = ScenePlan.model_validate({"scenes": [
        {"scene_id": "scene_001", "title": "模型自己起的标题", "location_id": "loc_001",
         "time": "黄昏",
         "beats": [{"type": "action", "text": "书房里。"}]},
    ]})
    script, artifacts = _run(monkeypatch, plan, raw="内景 · 林远家书房 · 夜\n")
    assert script.scenes[0].title == "模型自己起的标题"
    assert script.scenes[0].time == "黄昏"
    assert artifacts["source_backfill"] == {"titles": 0, "times": 0}


def test_原文不是剧本体裁时不动手():
    """小说体裁没有场次抬头行 → 不该硬套标题与昼夜。"""
    blocks = parse_screenplay_blocks("第一段 他走进屋子。屋里很黑。\n第二天他走了。")
    assert blocks == []
