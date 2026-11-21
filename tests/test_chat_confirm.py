# =====================================================================
# test_chat_confirm.py —— 流程分叉确认卡（D-76）：工具发起 + 确定性拍板
#
# 覆盖四段闭环：
#   1. ask_user_choice 发卡：PendingChoice 落库 + option_confirm payload，
#      选项 id 按位置重编（q{n}o{m}），发卡 ≠ 生效；
#   2. 护栏：坏 JSON / 合法选项不足 → 打回重发，不落卡（deepseek 格式容错）；
#   3. meta.intent=confirm_options 确定性拍板：回复含所选 label、落项目记忆、
#      单选题没选完 fail-closed（卡保持 pending）、重复确认幂等；
#   4. load_history 按 DB 回填卡片状态（刷新页面旧卡不骗人）。
# =====================================================================
import json

from app.chat.conductor import _handle_choice_confirm, load_history
from app.chat.tools import build_chat_tools


def _make_tools(store, llm):
    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    return tools, collector


QUESTIONS = json.dumps([
    {"question": "改编重点放在哪条线？", "multi": False, "options": [
        {"label": "替身悬疑线", "desc": "冲突集中", "recommended": True},
        {"label": "家庭情感线", "desc": "共情强"},
    ]},
    {"question": "先出哪几集？", "multi": True, "options": [
        {"label": "第 1 集"}, {"label": "第 2 集"},
    ]},
], ensure_ascii=False)


def _create_project_and_card(store, llm):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({
        "title": "替身", "adaptation_type": "short_drama",
        "raw_text": "他站在雨里等一通不会来的电话。" * 5,
    })
    pid = collector["project_id"]
    r = tools["ask_user_choice"].invoke({
        "project_id": pid, "title": "改编方向", "questions_json": QUESTIONS,
    })
    return tools, collector, pid, r


def test_ask_user_choice_registers_pending(store, llm):
    tools, collector, pid, r = _create_project_and_card(store, llm)
    assert "确认卡" in r and "改编方向" in r
    assert "没有任何决定生效" in r, "发卡 ≠ 生效，模型不得抢跑"

    card = collector["payloads"][-1]
    assert card["type"] == "option_confirm" and card["status"] == "pending"
    assert card["title"] == "改编方向"
    assert [q["question"] for q in card["questions"]] == ["改编重点放在哪条线？", "先出哪几集？"]
    # id 一律按位置重编：q1o1 / q2o2——确认回传按位置对齐，模型给不给 id 都行
    assert card["questions"][0]["options"][0]["id"] == "q1o1"
    assert card["questions"][1]["options"][1]["id"] == "q2o2"

    row = store.get_pending_choice(card["choice_id"])
    assert row is not None and row.status == "pending"
    assert row.questions[0]["options"][0]["label"] == "替身悬疑线"


def test_ask_user_choice_guardrails(store, llm):
    tools, collector = _make_tools(store, llm)
    bad_inputs = [
        "not-json",
        json.dumps([{"question": "q", "options": [{"label": "只有一项"}]}]),
        json.dumps([{"options": [{"label": "a"}, {"label": "b"}]}]),  # 缺问题文本
        "",
    ]
    for bad in bad_inputs:
        r = tools["ask_user_choice"].invoke({
            "project_id": "", "title": "x", "questions_json": bad,
        })
        assert "重发" in r, f"坏输入应打回重发：{bad!r}"
    assert collector["payloads"] == [], "护栏拦下的输入不应产生卡片"


def test_confirm_choice_happy_path(store, llm):
    tools, collector, pid, _r = _create_project_and_card(store, llm)
    card = collector["payloads"][-1]
    res = _handle_choice_confirm(store, conversation_id="", meta={
        "intent": "confirm_options", "choice_id": card["choice_id"],
        "choices": [
            {"question": "改编重点放在哪条线？", "option_ids": ["q1o1"]},
            {"question": "先出哪几集？", "option_ids": ["q2o1", "q2o2"]},
        ],
        "note": "第一集优先",
    })
    assert res is not None and "已确认" in res["reply"]
    assert "替身悬疑线" in res["reply"], "回复要拼出所选 label（下一轮 Agent 读历史用）"
    assert "第 1 集、第 2 集" in res["reply"]
    assert "第一集优先" in res["reply"], "用户备注要带进对话上下文"
    assert res["payloads"] == []

    assert store.get_pending_choice(card["choice_id"]).status == "confirmed"

    # 选择落项目记忆：跨会话不丢
    mems = store.list_memories(project_id=pid, kinds=["decision"])
    assert any("替身悬疑线" in m.content for m in mems)


def test_confirm_choice_fail_closed(store, llm):
    tools, collector, _pid, _r = _create_project_and_card(store, llm)
    card = collector["payloads"][-1]
    # 单选题没选：fail-closed，卡保持 pending，回复提示补选
    res = _handle_choice_confirm(store, conversation_id="", meta={
        "intent": "confirm_options", "choice_id": card["choice_id"],
        "choices": [{"question": "先出哪几集？", "option_ids": ["q2o1"]}],
    })
    assert "补选" in res["reply"]
    assert store.get_pending_choice(card["choice_id"]).status == "pending"


def test_confirm_choice_idempotent_and_missing(store, llm):
    tools, collector, _pid, _r = _create_project_and_card(store, llm)
    card = collector["payloads"][-1]
    full = {
        "intent": "confirm_options", "choice_id": card["choice_id"],
        "choices": [{"question": "改编重点放在哪条线？", "option_ids": ["q1o1"]},
                    {"question": "先出哪几集？", "option_ids": ["q2o1"]}],
    }
    assert "已确认" in _handle_choice_confirm(store, conversation_id="", meta=full)["reply"]
    # 再提交一次同一张卡：幂等，不再改状态
    res = _handle_choice_confirm(store, conversation_id="", meta=full)
    assert "已经确认过" in res["reply"]
    assert store.get_pending_choice(card["choice_id"]).status == "confirmed"
    # 不存在的卡
    res = _handle_choice_confirm(store, conversation_id="", meta={
        "intent": "confirm_options", "choice_id": "chc_nope", "choices": [],
    })
    assert "不存在" in res["reply"]


def test_load_history_resyncs_card_status(store, llm):
    tools, collector, _pid, _r = _create_project_and_card(store, llm)
    card = collector["payloads"][-1]
    thread = "conv_test"
    store.save_chat_message(thread_id=thread, role="assistant", content="请拍板",
                            payload=[dict(card)])
    # 拍板前：pending
    hist = load_history(store, thread)
    assert hist[0]["payloads"][0]["status"] == "pending"
    # 拍板后：load_history 按 DB 回填，旧卡不得再显示待选
    _handle_choice_confirm(store, conversation_id="", meta={
        "intent": "confirm_options", "choice_id": card["choice_id"],
        "choices": [{"question": "改编重点放在哪条线？", "option_ids": ["q1o1"]},
                    {"question": "先出哪几集？", "option_ids": ["q2o1"]}],
    })
    hist = load_history(store, thread)
    assert hist[0]["payloads"][0]["status"] == "confirmed"
