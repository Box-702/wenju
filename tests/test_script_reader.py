# =====================================================================
# test_script_reader.py —— read_script：Agent 读自己的作品文档
#
# 对标 mediago-drama C1（docs/PLAN-mediago-reference.md）：此前 Agent 只能
# 读小说原文，读不到剧本正文/镜头/视觉资产——「把林墨那场戏改一下」时
# 它根本不知道那场戏现在什么样。这个工具补的就是这条链路。
# =====================================================================

from __future__ import annotations

import pytest

from app.chat.tools import build_chat_tools
from app.chat.tools.context import Collector
from app.domain import Script


def _collector() -> Collector:
    c: Collector = {"payloads": []}
    return c


def _tools(store, llm, project_id=None):
    return {t.name: t for t in build_chat_tools(store, llm, _collector(), None, project_id)}


@pytest.fixture()
def project_with_script(store, llm, sample_text):
    """建一个有两场戏剧本的项目（人物名「林然」「阿姐」，与 conftest 原文一致）。"""
    p = store.create_project(
        title="雨夜", adaptation_type="short_drama", language="zh", raw_text=sample_text
    )
    script = Script.model_validate({
        "title": "雨夜",
        "logline": "林然在旧楼赴约",
        "language": "zh-CN",
        "source": {"chapter_count": 3, "chapter_ids": ["ch_001", "ch_002", "ch_003"]},
        "characters": [
            {"id": "char_lingran", "name": "林然", "gender": "male"},
            {"id": "char_ajie", "name": "阿姐", "gender": "female"},
        ],
        "locations": [
            {"id": "loc_oldbuilding", "name": "旧楼", "description": "拆迁前的旧货市场"},
        ],
        "scenes": [
            {
                "id": "scene_001",
                "title": "旧楼赴约",
                "chapter_refs": ["ch_001"],
                "location_id": "loc_oldbuilding",
                "characters": ["char_lingran"],
                "purpose": "见到阿姐",
                "conflict": "阿姐不认他",
                "beats": [
                    {"id": "beat_001", "type": "action", "text": "林然熄火，雨水顺着挡风玻璃淌下"},
                    {"id": "beat_002", "type": "dialogue", "speaker": "char_ajie", "line": "你终于来了。", "emotion": "冷淡"},
                ],
            },
            {
                "id": "scene_002",
                "title": "照片对峙",
                "chapter_refs": ["ch_001"],
                "location_id": "loc_oldbuilding",
                "characters": ["char_lingran", "char_ajie"],
                "purpose": "看清照片",
                "conflict": "红雨衣男人抢先一步",
                "beats": [
                    {"id": "beat_003", "type": "action", "text": "角落里红雨衣把照片按在桌上"},
                ],
            },
        ],
    })
    store.create_version(p, script, source_type="generation", label="初稿")
    return p


def test_outline_lists_every_scene(store, llm, project_with_script):
    tools = _tools(store, llm)
    out = tools["read_script"].invoke({"project_id": project_with_script.id, "part": "outline"})
    assert "共 2 场" in out
    assert "scene_001" in out and "旧楼赴约" in out
    assert "林然" in out  # 人物名已解析成人话，不是裸 id


def test_scene_query_hits_by_person_or_title(store, llm, project_with_script):
    tools = _tools(store, llm)
    pid = project_with_script.id
    # 标题命中一场：直接给台本
    out = tools["read_script"].invoke({"project_id": pid, "part": "scenes", "query": "照片对峙"})
    assert "beat_003" in out and "红雨衣" in out
    # 人物名命中一场
    out = tools["read_script"].invoke({"project_id": pid, "part": "scenes", "query": "阿姐"})
    assert "scene_002" in out
    # 人物名命中两场：列候选而不是猜（与 rekey 的 ambiguous 不猜同一原则）
    out = tools["read_script"].invoke({"project_id": pid, "part": "scenes", "query": "林然"})
    assert "命中 2 场" in out and "scene_001" in out


def test_scene_output_keeps_beat_ids(store, llm, project_with_script):
    """台本里保留 beat id —— 它是 run_adaptation 定位改动的把手。"""
    tools = _tools(store, llm)
    out = tools["read_script"].invoke({
        "project_id": project_with_script.id, "part": "scenes", "query": "旧楼赴约",
    })
    assert "beat_001" in out and "beat_002" in out
    assert "你终于来了" in out
    assert "阿姐（冷淡）" in out  # 说话人解析成名字 + 情绪


def test_no_match_names_the_real_options(store, llm, project_with_script):
    tools = _tools(store, llm)
    out = tools["read_script"].invoke({
        "project_id": project_with_script.id, "part": "scenes", "query": "不存在的角色",
    })
    assert "没有匹配" in out and "旧楼赴约" in out


def test_shots_reports_approval_and_video_state(store, llm, project_with_script):
    p = project_with_script
    store.create_video_version(
        project_id=p.id,
        shots=[
            {
                "id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
                "shot_type": "close_up", "duration_sec": 4, "subject": "林然的侧脸",
                "action": "抬眼看旧楼", "prompt_status": "approved",
                "camera": {"type": "push_in", "path": "从车窗缓缓推到侧脸"},
                "final_frame": "他推门下车",
            },
            {
                "id": "shot_scene_001_001", "scene_id": "scene_001", "order": 1,
                "shot_type": "medium", "duration_sec": 5, "prompt_status": "needs_review",
            },
        ],
        source_type="agent",
    )
    tools = _tools(store, llm)
    out = tools["read_script"].invoke({"project_id": p.id, "part": "shots"})
    assert "共 2 镜" in out
    assert "已批准" in out and "待审" in out
    assert "push_in" in out and "他推门下车" in out


def test_style_reports_visual_assets_by_name(store, llm, project_with_script):
    p = project_with_script
    store.merge_video_style(p.id, {
        "visual_style": "realistic",
        "color_palette": ["#101820"],
        "lighting_style": "低调暖光",
        "character_appearances": {"char_lingran": "三十岁男性，深灰夹克，眼神疲惫"},
        "reference_images": {"char_lingran": "http://x/img.png"},
    })
    tools = _tools(store, llm)
    out = tools["read_script"].invoke({"project_id": p.id, "part": "style"})
    assert "低调暖光" in out
    assert "林然｜造型：三十岁男性" in out and "定妆图：有" in out
    assert "阿姐｜（未登记）" in out


def test_project_id_defaults_to_current_conversation(store, llm, project_with_script):
    """模型经常漏传 project_id——缺省必须落回本轮作品而不是报错。"""
    tools = _tools(store, llm, project_id=project_with_script.id)
    out = tools["read_script"].invoke({"part": "outline"})
    assert "共 2 场" in out


def test_no_script_version_says_so(store, llm):
    p = store.create_project(title="空", adaptation_type="short_drama", language="zh", raw_text="x")
    tools = _tools(store, llm)
    out = tools["read_script"].invoke({"project_id": p.id, "part": "outline"})
    assert "还没有剧本版本" in out


def test_unknown_part_lists_valid_options(store, llm, project_with_script):
    tools = _tools(store, llm)
    out = tools["read_script"].invoke({"project_id": project_with_script.id, "part": "bogus"})
    assert "scenes" in out and "outline" in out


def test_long_scene_output_truncates_with_notice(store, llm, project_with_script):
    """超长必须截尾并明说——静默截断比诚实的「略」危险（覆盖诚实化同款原则）。"""
    from app.domain import ScriptBeat

    p = project_with_script
    script = store.latest_version(p).script.model_copy(deep=True)
    sc = script.scenes[0]
    # 塞 40 个长动作节拍：总长远超 6000 上限
    sc.beats.extend(
        ScriptBeat(id=f"beat_{i:03d}", type="action", text="很长的动作描写，" * 60)
        for i in range(100, 140)
    )
    store.create_version(p, script, source_type="manual_edit", label="加料")
    tools = _tools(store, llm)
    out = tools["read_script"].invoke({"project_id": p.id, "part": "scenes", "query": "旧楼赴约"})
    assert "截断" in out
