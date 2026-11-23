# =====================================================================
# test_project_switch.py —— 「切换作品」技能的两个工具
#
# 背景（D-XX）：换作品从输入框的下拉按钮改成对话里的技能引用。
# 后端这一侧要保证两件事：
#   1. 匹配不到 / 匹配到多部时**不猜**——回候选，让模型问用户；
#   2. 匹配唯一时真的换：会话改挂到目标作品，本轮上下文的 project_id 也换。
# =====================================================================

from __future__ import annotations

import pytest

from app.chat.tools import build_chat_tools


def _make(store, llm, conversation_id="conv_a", project_id=None):
    collector = {"project_id": project_id, "payloads": [], "conversation_id": conversation_id}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector, project_id=project_id)}
    return tools, collector


def _two_projects(store):
    a = store.create_project(title="孔乙己", raw_text="……",
                             adaptation_type="short_drama", language="zh-CN")
    b = store.create_project(title="果园对白", raw_text="……",
                             adaptation_type="short_drama", language="zh-CN")
    return a, b


def test_list_projects_shows_titles_and_ids(store, llm):
    tools, _ = _make(store, llm)
    a, b = _two_projects(store)
    out = tools["list_projects"].invoke({})
    assert "孔乙己" in out and "果园对白" in out
    assert a.id in out and b.id in out


def test_list_projects_on_empty_library(store, llm):
    tools, _ = _make(store, llm)
    out = tools["list_projects"].invoke({})
    assert "没有" in out


def test_switch_exact_title_moves_conversation_and_context(store, llm):
    tools, collector = _make(store, llm)
    a, b = _two_projects(store)
    conv = store.create_conversation(a.id, title="对话")
    collector["conversation_id"] = conv.id
    collector["project_id"] = a.id

    out = tools["switch_project"].invoke({"title": "果园对白"})
    assert "已切到" in out and "果园对白" in out
    # 本轮上下文换了
    assert collector["project_id"] == b.id
    # 会话改挂了（id 不变，归属换掉）
    moved = store.get_conversation(conv.id)
    assert moved is not None and moved.project_id == b.id
    assert moved.id == conv.id


def test_switch_matches_unique_substring(store, llm):
    """用户口语只说半截剧名（「切到果园」）：唯一包含就该认。"""
    tools, collector = _make(store, llm)
    a, b = _two_projects(store)
    collector["project_id"] = a.id
    out = tools["switch_project"].invoke({"title": "果园"})
    assert "已切到" in out
    assert collector["project_id"] == b.id


def test_switch_refuses_when_ambiguous(store, llm):
    """两部作品都像（且没有完全同名的）：绝不猜，回候选。

    注意「完全同名」不算歧义——有一个叫《孔乙己》、另一个叫《孔乙己续》时，
    精确匹配优先，切《孔乙己》是对的。"""
    tools, collector = _make(store, llm)
    store.create_project(title="替身上卷", raw_text="……",
                         adaptation_type="short_drama", language="zh-CN")
    store.create_project(title="替身下卷", raw_text="……",
                         adaptation_type="short_drama", language="zh-CN")
    a = store.create_project(title="果园对白", raw_text="……",
                             adaptation_type="short_drama", language="zh-CN")
    collector["project_id"] = a.id

    out = tools["switch_project"].invoke({"title": "替身"})
    assert "已切到" not in out
    assert "替身上卷" in out and "替身下卷" in out
    assert collector["project_id"] == a.id  # 没动


def test_switch_prefers_exact_title_over_longer_one(store, llm):
    """《孔乙己》与《孔乙己续》并存：精确命中优先，不算歧义。"""
    tools, collector = _make(store, llm)
    exact = store.create_project(title="孔乙己", raw_text="……",
                                 adaptation_type="short_drama", language="zh-CN")
    store.create_project(title="孔乙己续", raw_text="……",
                         adaptation_type="short_drama", language="zh-CN")
    other = store.create_project(title="果园对白", raw_text="……",
                                  adaptation_type="short_drama", language="zh-CN")
    collector["project_id"] = other.id

    out = tools["switch_project"].invoke({"title": "孔乙己"})
    assert "已切到" in out
    assert collector["project_id"] == exact.id


def test_switch_accepts_project_id(store, llm):
    """同名作品场景：模型拿 list_projects 的 id 直接切（2026-11-22 浏览器实测补）。"""
    tools, collector = _make(store, llm)
    a = store.create_project(title="果园对白", raw_text="……",
                             adaptation_type="short_drama", language="zh-CN")
    b = store.create_project(title="果园对白", raw_text="……",
                             adaptation_type="short_drama", language="zh-CN")
    collector["project_id"] = a.id

    out = tools["switch_project"].invoke({"title": b.id})
    assert "已切到" in out
    assert collector["project_id"] == b.id


def test_switch_reports_when_nothing_matches(store, llm):
    tools, collector = _make(store, llm)
    a, _ = _two_projects(store)
    collector["project_id"] = a.id
    out = tools["switch_project"].invoke({"title": "西游记"})
    assert "没有找到" in out
    assert collector["project_id"] == a.id


def test_switch_empty_title_asks_back(store, llm):
    tools, collector = _make(store, llm)
    a, _ = _two_projects(store)
    collector["project_id"] = a.id
    out = tools["switch_project"].invoke({"title": "   "})
    assert "没听清" in out
    assert collector["project_id"] == a.id


def test_switch_to_current_project_is_a_noop(store, llm):
    tools, collector = _make(store, llm)
    a, _ = _two_projects(store)
    collector["project_id"] = a.id
    out = tools["switch_project"].invoke({"title": "孔乙己"})
    assert "已经在" in out
    assert collector["project_id"] == a.id


# ---------- store 层的改挂 ----------


def test_move_conversation_to_project(store):
    a, b = _two_projects(store)
    conv = store.create_conversation(a.id, title="对话")
    moved = store.move_conversation_to_project(conv.id, b.id)
    assert moved is not None and moved.project_id == b.id
    assert store.get_conversation(conv.id).project_id == b.id


def test_move_conversation_rejects_unknown_project(store):
    a, _ = _two_projects(store)
    conv = store.create_conversation(a.id, title="对话")
    with pytest.raises(ValueError):
        store.move_conversation_to_project(conv.id, "proj_nope")


def test_move_conversation_unknown_conversation(store):
    _, b = _two_projects(store)
    assert store.move_conversation_to_project("conv_nope", b.id) is None
