# =====================================================================
# test_entity_assets.py —— 资产按实体 id 索引（而不是按名字）
#
# 病根：角色外貌 / 环境描述 / 定妆图 / 音色 / 性别五份数据全部以**中文名**做键。
# 名字是用户随时会改的，改一次名五份锚点同时失联——而且不报错，只是悄悄当没有。
#
# 这里锁住四条性质：
#   1. 新数据一律按 id 存（落库前由代码把名字换成 id，不指望模型写 id）；
#   2. 老数据（名字键）读得动——不需要一次性迁移；
#   3. **改名之后锚点还在**（这是整件事的目的）；
#   4. 重名不猜（猜错等于把 A 的脸贴到 B 身上，比没有更糟）。
# =====================================================================

from __future__ import annotations

import pytest

from app.domain import Character, Location, Scene, Script, ScriptBeat, Source, StyleGuide
from app.media.assets import (
    as_name_map,
    build_index,
    lookup,
    normalize_style,
    rekey,
)
from app.media.casting import cast_script
from app.media.library import apply_library_patch, build_library


def _script(name: str = "孔乙己") -> Script:
    return Script(
        title="测试",
        source=Source(chapter_count=1, chapter_ids=["ch_001"]),
        logline="测",
        characters=[
            Character(id="char_001", name=name, role="protagonist"),
            Character(id="char_002", name="掌柜", role="supporting"),
        ],
        locations=[Location(id="loc_001", name="咸亨酒店")],
        scenes=[
            Scene(
                id="scene_001", title="柜台", chapter_refs=["ch_001"], location_id="loc_001",
                characters=["char_001", "char_002"], purpose="p", conflict="c",
                beats=[ScriptBeat(id="beat_001", type="dialogue", speaker="char_001", line="温两碗酒。")],
            )
        ],
    )


# ---------- 基础解析 ----------


def test_名字键换成_id_键():
    idx = build_index(_script())
    out = rekey({"孔乙己": "长衫", "咸亨酒店": "木柜台"}, idx)
    assert out == {"char_001": "长衫", "loc_001": "木柜台"}


def test_rekey_幂等且_id_键优先():
    idx = build_index(_script())
    data = {"char_001": "新锚", "孔乙己": "旧锚"}
    out = rekey(data, idx)
    assert out == {"char_001": "新锚"}
    assert rekey(out, idx) == out


def test_解析不出来的键原样保留():
    """剧本里删掉的角色、模型写错的别名：不能因为对不上就把用户数据丢了。"""
    idx = build_index(_script())
    assert rekey({"不存在的人": "锚"}, idx) == {"不存在的人": "锚"}


def test_重名不猜():
    script = _script()
    script.characters.append(Character(id="char_003", name="孔乙己", role="other"))
    idx = build_index(script)
    assert idx.resolve("孔乙己") is None
    assert rekey({"孔乙己": "锚"}, idx) == {"孔乙己": "锚"}


def test_名字与_id_都能查():
    script = _script()
    idx = build_index(script)
    assert idx.resolve("孔乙己") == "char_001"
    assert idx.resolve("char_001") == "char_001"
    # 「酒客 甲」和「酒客甲」是同一个人
    assert build_index(_script("酒客 甲")).resolve("酒客甲") == "char_001"


def test_提示词用显示名而不是_id():
    """视频模型不认识 char_004，只认识「孔乙己」。"""
    idx = build_index(_script())
    assert as_name_map({"char_001": "四十岁上下"}, idx) == {"孔乙己": "四十岁上下"}


# ---------- 改名之后锚点还在（核心目的）----------


def test_改名之后外貌音色定妆图都还在():
    style = {
        "character_appearances": {"char_001": "旧长衫，青白脸色"},
        "reference_images": {"char_001": "https://img/1.png"},
        "voice_casting": {"char_001": "male-qn-badao"},
        "character_genders": {"char_001": "male"},
    }
    renamed = _script("孔乙己先生")     # 用户把名字改了

    lib = build_library(renamed, style)
    row = next(c for c in lib["characters"] if c["char_id"] == "char_001")
    assert row["appearance"] == "旧长衫，青白脸色"
    assert row["image"] == "https://img/1.png"
    assert row["voice_id"] == "male-qn-badao"
    assert row["confirmed"] is True


def test_老数据按名字存的也读得动():
    """不要求一次性迁移：老项目里存的是名字键，读的时候自动认。"""
    style = {
        "character_appearances": {"孔乙己": "旧长衫"},
        "reference_images": {"孔乙己": "https://img/1.png", "咸亨酒店": "https://img/2.png"},
        "voice_casting": {"孔乙己": "male-qn-daxuesheng"},
    }
    lib = build_library(_script(), style)
    row = next(c for c in lib["characters"] if c["char_id"] == "char_001")
    loc = next(l for l in lib["locations"] if l["loc_id"] == "loc_001")
    assert row["appearance"] == "旧长衫"
    assert row["voice_id"] == "male-qn-daxuesheng"
    assert loc["image"] == "https://img/2.png"


def test_normalize_style_不改入参():
    style = {"character_appearances": {"孔乙己": "旧长衫"}}
    normalize_style(style, _script())
    assert style["character_appearances"] == {"孔乙己": "旧长衫"}


# ---------- 人工修改走名字，落库存 id ----------


def test_资产库改音色后存的是_id_键():
    style: dict = {}
    merged = apply_library_patch(
        style, {"characters": {"孔乙己": {"voice_id": "male-qn-jingying"}}}, _script(),
    )
    assert merged["voice_casting"] == {"char_001": "male-qn-jingying"}


def test_资产库改场景描述后存的是_id_键():
    merged = apply_library_patch(
        {}, {"locations": {"咸亨酒店": {"description": "木柜台，油灯"}}}, _script(),
    )
    assert merged["environment_descriptions"] == {"loc_001": "木柜台，油灯"}


# ---------- 美术指导落库：名字换成 id ----------


def test_美术指导解析后按_id_存():
    from app.crew.art_director import ArtDirectorAgent

    raw = {
        "color_palette": ["#000"],
        "character_appearances": {"孔乙己": "旧长衫", "掌柜": "圆脸"},
        "environment_descriptions": {"咸亨酒店": "曲尺形大柜台"},
    }
    guide = ArtDirectorAgent._parse_guide(raw, _script())
    assert set(guide.character_appearances) == {"char_001", "char_002"}
    assert set(guide.environment_descriptions) == {"loc_001"}
    assert guide.character_appearances["char_001"] == "旧长衫"


# ---------- 定妆图注册与回填 ----------


def test_定妆图按_id_注册并回填到镜头():
    from app.domain import Shot
    from app.media.refs import apply_reference_images

    script = _script()
    guide = StyleGuide(
        character_appearances={"char_001": "旧长衫", "char_002": "圆脸"},
        environment_descriptions={"loc_001": "木柜台"},
    )
    # 起点：参考图注册表用名字键（老数据），回填要能认出来
    refs = {"孔乙己": "https://img/char.png", "咸亨酒店": "https://img/loc.png"}
    shot = Shot(id="shot_scene_001_000", scene_id="scene_001", order=0, duration_sec=4, action="站着")
    filled = apply_reference_images([shot], refs, script)
    assert filled == 1
    assert "https://img/loc.png" in shot.reference_images
    assert "https://img/char.png" in shot.reference_images
    assert guide  # 只是确认构造可用


@pytest.mark.parametrize("key", ["char_001", "孔乙己"])
def test_lookup_两种键都能命中(key):
    idx = build_index(_script())
    assert lookup({key: "v"}, "char_001", idx) == "v"


def test_第一次改资产时整份老数据一起迁移():
    """老项目只要用户改过一次资产，全表就变成 id 键——之后改名才真正安全。

    否则只有被改到的那个角色是 id 键，其余仍是名字键，改个名照样丢。
    """
    style = {
        "character_appearances": {"孔乙己": "旧长衫", "掌柜": "圆脸"},
        "environment_descriptions": {"咸亨酒店": "木柜台"},
        "reference_images": {"掌柜": "https://img/2.png"},
    }
    merged = apply_library_patch(
        style, {"characters": {"孔乙己": {"voice_id": "male-qn-badao"}}}, _script(),
    )
    assert set(merged["character_appearances"]) == {"char_001", "char_002"}
    assert set(merged["environment_descriptions"]) == {"loc_001"}
    assert set(merged["reference_images"]) == {"char_002"}
    # 未被改动的值一个都没丢
    assert merged["character_appearances"]["char_002"] == "圆脸"


# ---------- 美术指导：画风锚护栏（deepseek 给 anime 写实拍纹理的实测坑） ----------


def test_动画画风下写出实拍纹理锚被检出():
    from app.crew.art_director import ArtDirectorAgent
    from app.domain import StyleGuide

    guide = StyleGuide(
        visual_style="anime",
        character_appearances={
            "char_001": "中年男性，肤色蜡黄带真实皮肤毛孔与细微油光，雀斑明显，细长眼",
            "char_002": "十五六岁少年，黑色短发，灰蓝短打，线条干净",
        },
    )
    mismatch = ArtDirectorAgent._style_mismatch(guide, "anime")
    assert mismatch and "char_001" in mismatch
    assert "毛孔" in mismatch  # 修正指令要点名串味词


def test_写实画风不误伤_动画单纹理词也不冤枉():
    from app.crew.art_director import ArtDirectorAgent
    from app.domain import StyleGuide

    # 写实：纹理词本来就该有
    realistic = StyleGuide(
        visual_style="realistic",
        character_appearances={"char_001": "皮肤毛孔清晰，风吹日晒的粗糙质感"},
    )
    assert ArtDirectorAgent._style_mismatch(realistic, "realistic") is None

    # 动画：单个「粗糙」（布料）是正当描述，不算串味
    anime_ok = StyleGuide(
        visual_style="anime",
        character_appearances={"char_001": "土黄粗布短褂，布料粗糙，发色深棕"},
    )
    assert ArtDirectorAgent._style_mismatch(anime_ok, "anime") is None


def test_质检问题里带画风标签():
    """定妆图质检必须知道项目该长什么样——五张五种画风的事故就是这么漏进来的。"""
    from app.media.refs import _QA_CHARACTER, _style_label

    q = _QA_CHARACTER.format(expect="少年", style=_style_label("anime"))
    assert "二次元（漫剧）" in q and "真人照片感" in q
    assert _style_label(None) == "写实实拍"
