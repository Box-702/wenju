# =====================================================================
# test_chat.py —— 对话式 Agent 测试（无 RAG 版）
#
# 无模型 key 环境下验证对话工具全链路。
# =====================================================================

from app.chat import (
    _handle_resume,
    build_chat_tools,
    chat_once,
    load_history,
    main_skill_names,
    system_prompt,
)


def _make_tools(store, llm):
    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    return tools, collector


def test_chat_tool_flow_closed_loop(store, llm, settings, sample_text):
    tools, collector = _make_tools(store, llm)

    # 1) 对话式「新建作品」
    r = tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "短剧", "raw_text": sample_text})
    assert "已创建作品《雨夜》" in r
    pid = collector["project_id"]
    assert pid and store.get_project(pid)

    # 2) 生成初稿
    r = tools["generate_script"].invoke({"project_id": pid})
    assert "已生成初稿" in r
    version = store.latest_version(store.get_project(pid))
    assert version is not None and version.source_type == "generation"

    # 3) 改编（底层工作流跑到 review 中断）
    r = tools["run_adaptation"].invoke({"project_id": pid, "instruction": "把对白改口语一点"})
    assert "改动建议" in r
    assert collector["payloads"], "应产生 patch_review 载荷"
    review = collector["payloads"][0]
    assert review["type"] == "patch_review" and review["run_id"]
    assert review["patch"], "无模型回退也应产出可审阅 patch"
    assert store.get_agent_run(review["run_id"]).status == "reviewing"

    # 4) 审阅：接受全部 -> 生成新版本
    res = _handle_resume(
        store, llm, settings,
        run_id=review["run_id"], action="accept", patch_indexes=None, feedback=None, patch=None,
    )
    assert res["payloads"][0]["type"] == "version_applied", res
    new_vid = res["payloads"][0]["version_id"]
    new_version = store.get_version(new_vid)
    assert new_version.source_type == "agent_adaptation"


def test_chat_tool_reject(store, llm, settings, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text})
    pid = collector["project_id"]
    tools["generate_script"].invoke({"project_id": pid})
    tools["run_adaptation"].invoke({"project_id": pid, "instruction": "改紧凑"})
    run_id = collector["payloads"][0]["run_id"]

    res = _handle_resume(
        store, llm, settings,
        run_id=run_id, action="reject", patch_indexes=None, feedback=None, patch=None,
    )
    assert "拒绝" in res["reply"]
    assert store.get_agent_run(run_id).status == "rejected"

    # 防重：再次操作应提示已处理
    res2 = _handle_resume(
        store, llm, settings,
        run_id=run_id, action="accept", patch_indexes=None, feedback=None, patch=None,
    )
    assert "已处理过" in res2["reply"]


def test_chat_once_persists_history(store, llm, settings, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text})
    pid = collector["project_id"]
    conv = store.ensure_default_conversation(pid)
    assert conv.project_id == pid

    # 无模型：conductor 回退提示，但消息应持久化、历史可读。
    out = chat_once(store, llm, settings,
                    conversation_id=conv.id, project_id=pid, message="你好", meta=None)
    assert out["thread_id"] == conv.id
    assert "没有配置对话模型" in out["reply"]

    hist = load_history(store, conv.id)
    assert len(hist) == 2
    assert hist[0]["role"] == "user" and hist[0]["content"] == "你好"
    assert hist[1]["role"] == "assistant"

    # 全局线程（未选作品时的会话）与作品对话隔离。
    assert load_history(store, None) == []


def test_project_multiple_conversations_isolated(store, llm, settings, sample_text):
    """一个项目下多个对话：上下文与历史彼此隔离。"""
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text})
    pid = collector["project_id"]

    conv_a = store.create_conversation(pid, title="对话A")
    conv_b = store.create_conversation(pid, title="对话B")
    assert len(store.list_conversations(pid)) >= 2

    chat_once(store, llm, settings,
              conversation_id=conv_a.id, project_id=pid, message="在A里说", meta=None)
    assert len(load_history(store, conv_a.id)) == 2
    assert len(load_history(store, conv_b.id)) == 0, "B 对话不应看到 A 的消息"

    # 重命名 / 删除
    renamed = store.rename_conversation(conv_b.id, "对话B-改名")
    assert renamed.title == "对话B-改名"
    assert store.delete_conversation(conv_b.id)
    assert store.get_conversation(conv_b.id) is None


def test_chat_resume_via_meta(store, llm, settings, sample_text):
    """前端审阅按钮走后端 chat 接口（meta.intent=resume）也应闭环。"""
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text})
    pid = collector["project_id"]
    conv = store.ensure_default_conversation(pid)
    tools["generate_script"].invoke({"project_id": pid})
    tools["run_adaptation"].invoke({"project_id": pid, "instruction": "改节奏"})
    run_id = collector["payloads"][0]["run_id"]

    out = chat_once(
        store, llm, settings,
        conversation_id=conv.id, project_id=pid, message="",
        meta={"intent": "resume", "run_id": run_id, "action": "accept", "patch_indexes": None},
    )
    assert out["payloads"][0]["type"] == "version_applied", out
    assert "已接受" in out["reply"]
    hist = load_history(store, conv.id)
    assert any(m["role"] == "user" and "接受" in m["content"] for m in hist)


# ---------- 视频侧对话工具 + 后台任务查询 ----------


def _make_video_project(store, llm, sample_text):
    """造一个带镜头方案的项目：两个镜头，提示词一待审一已批准。"""
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text})
    pid = collector["project_id"]
    store.create_video_version(project_id=pid, shots=[
        {"id": "shot_scene001_000", "subject": "林然进屋", "video_prompt": "A man enters.", "prompt_status": "draft"},
        {"id": "shot_scene001_001", "subject": "红雨衣男人", "video_prompt": "A man in red raincoat.", "prompt_status": "approved"},
    ], source_type="agent", label="导演拆解")
    return tools, collector, pid


def test_get_status_reports_shots(store, llm, sample_text):
    """查询只有一个入口：概况给计数，detail=shots 才展开逐镜清单。"""
    tools, _, pid = _make_video_project(store, llm, sample_text)
    summary = tools["get_status"].invoke({"project_id": pid})
    assert "已批准 1" in summary
    assert "shot_scene001_000" not in summary       # 概况不逐镜展开
    full = tools["get_status"].invoke({"project_id": pid, "detail": "shots"})
    assert "shot_scene001_000" in full and "待审" in full


def test_approve_video_prompts_creates_version(store, llm, sample_text):
    tools, _, pid = _make_video_project(store, llm, sample_text)
    r = tools["approve_video_prompts"].invoke({"project_id": pid, "shot_ids": "shot_scene001_000"})
    assert "已批准 1 个" in r
    versions = sorted(store.list_video_versions(pid), key=lambda v: v.created_at)
    shot = next(s for s in versions[-1].shots if s["id"] == "shot_scene001_000")
    assert shot["prompt_status"] == "approved"
    # 已批准的再批一次：无可批准
    r2 = tools["approve_video_prompts"].invoke({"project_id": pid, "shot_ids": "shot_scene001_000"})
    assert "没有可批准" in r2


def test_get_status_before_any_job_and_rerun_unknown(store, llm, sample_text):
    tools, _, pid = _make_video_project(store, llm, sample_text)
    assert "已出片 0" in tools["get_status"].invoke({"project_id": pid})
    assert "没有找到镜头" in tools["rerun_video"].invoke({"project_id": pid, "shot_id": "shot_x"})


def test_submit_videos_requires_approval(store, llm, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text})
    pid = collector["project_id"]
    store.create_video_version(project_id=pid, shots=[
        {"id": "shot_scene001_000", "video_prompt": "A man.", "prompt_status": "draft"},
    ], source_type="agent")
    r = tools["submit_videos"].invoke({"project_id": pid})
    assert "需要先经过人工批准" in r


def test_get_status_includes_background_tasks(store, llm, sample_text):
    from app.agent.tasks import SubAgentRunner, SubAgentTask
    from app.chat import build_chat_tools

    runner = SubAgentRunner()
    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector, runner=runner)}
    tools["create_project"].invoke({"title": "雨夜", "raw_text": sample_text})
    pid = collector["project_id"]
    runner._tasks["task_x"] = SubAgentTask(id="task_x", name="导演场景拆解", status="done", result="已拆出 4 个镜头")
    r = tools["get_status"].invoke({"project_id": pid})
    assert "导演场景拆解" in r and "已完成" in r and "4 个镜头" in r


def test_trajectory_note_compacts_events():
    from app.chat.conductor import _trajectory_note

    events = [
        {"type": "tool_call", "name": "breakdown_scenes"},
        {"type": "tool_result", "name": "breakdown_scenes", "summary": "已拆出 4 个镜头"},
    ]
    note = _trajectory_note(events)
    assert note.startswith("［本轮动作］")
    assert "拆解镜头：已拆出 4 个镜头" in note
    assert "（未返回）" not in note
    # 只有调用没有结果 → 标未返回，让模型知道上轮没拿到结果
    assert "（未返回）" in _trajectory_note([{"type": "tool_call", "name": "ask"}])


def test_reasoning_delta_reads_additional_kwargs():
    from types import SimpleNamespace
    from app.chat.loop import _reasoning_delta

    assert _reasoning_delta(SimpleNamespace(additional_kwargs={"reasoning_content": "想"})) == "想"
    assert _reasoning_delta(SimpleNamespace(additional_kwargs={})) == ""


# ---------- 三层记忆 + 会话滚动摘要 ----------


def test_memory_scope_visibility(store):
    """global 全可见；project/conversation 只对归属上下文可见，按 global→project→conversation 排序。"""
    from app.store import Memory

    store.add_memory(kind="preference", scope="global", content="喜欢克制结尾")
    store.add_memory(kind="decision", scope="project", project_id="p1", content="城市定为赛博朋克")
    store.add_memory(kind="decision", scope="project", project_id="p2", content="别的项目")
    store.add_memory(kind="feedback", scope="conversation", project_id="p1", conversation_id="c1", content="别用倒叙")

    rows_p1 = store.list_memories(project_id="p1", conversation_id="c1")
    assert [m.scope for m in rows_p1] == ["global", "project", "conversation"]
    assert "别的项目" not in [m.content for m in rows_p1]
    # 无项目上下文：只剩全局
    assert [m.content for m in store.list_memories()] == ["喜欢克制结尾"]


def test_learn_from_decision(store):
    from app.memory import learn_from_decision

    learn_from_decision(store, project_id="p1", conversation_id="c1",
                        action="accept", instruction="改口语化", patch_count=3)
    learn_from_decision(store, project_id="p1", conversation_id="c1",
                        action="regenerate", instruction="改节奏", feedback="太拖了")
    rows = store.list_memories(project_id="p1", conversation_id="c1")
    kinds = {m.kind: m.content for m in rows}
    assert "改口语化" in kinds["decision"] and "3 处" in kinds["decision"]
    assert "太拖了" in kinds["feedback"]
    # 无反馈的拒绝不产生记忆
    learn_from_decision(store, project_id="p1", conversation_id="c1", action="reject", instruction="x")
    assert len(store.list_memories(project_id="p1", conversation_id="c1")) == 2


def test_format_memory_note(store):
    from app.memory import format_memory_note

    assert format_memory_note(store, project_id=None, conversation_id=None) == ""
    store.add_memory(kind="preference", scope="global", content="喜欢短句")
    note = format_memory_note(store, project_id="p1", conversation_id="c1")
    assert "[用户记忆]" in note and "喜欢短句" in note


def test_compress_history_truncates_without_llm(store):
    """无模型（available=False）：超过窗口回落为保留最近 N 条，不写摘要。"""
    from app.memory import RECENT_KEEP, compress_history_if_needed

    for i in range(RECENT_KEEP + 5):
        store.save_chat_message(thread_id="t1", role="user", content=f"消息{i}", payload=[], events=[])
    summary, recent = compress_history_if_needed(store, type("L", (), {"available": False})(), "t1")
    assert summary == ""
    assert len(recent) == RECENT_KEEP
    assert recent[-1].content == f"消息{RECENT_KEEP + 4}"
    assert store.get_conversation("t1") is None or not (store.get_conversation("t1").summary or {})


def test_compress_history_rolls_summary(store):
    """假 LLM：达到阈值时压缩旧消息，摘要落库，covered_count 正确。"""
    from app.memory import RECENT_KEEP, compress_history_if_needed

    class _FakeLLM:
        available = True

        class _Chat:
            def invoke(self, msgs):
                return type("R", (), {"content": "事实清单：用户在改剧本"})()

        def chat(self):
            return self._Chat()

    conv = store.create_conversation(project_id=None, title="长对话")
    total = RECENT_KEEP + 12  # 超过触发线
    for i in range(total):
        store.save_chat_message(thread_id=conv.id, role="user", content=f"消息{i}", payload=[], events=[])
    summary, recent = compress_history_if_needed(store, _FakeLLM(), conv.id)
    assert "事实清单" in summary
    assert len(recent) == RECENT_KEEP
    conv = store.get_conversation(conv.id)
    assert conv.summary["covered_count"] == total - RECENT_KEEP
    # 攒不够新内容时沿用旧摘要，不再调 LLM
    summary2, _ = compress_history_if_needed(store, _FakeLLM(), conv.id)
    assert summary2 == summary


def test_compress_survives_llm_failure(store, caplog):
    """摘要 LLM 抛错：回落截断，不抛异常、不写坏摘要。"""
    import pytest
    from app.memory import RECENT_KEEP, compress_history_if_needed

    class _BadLLM:
        available = True

        class _Chat:
            def invoke(self, msgs):
                raise RuntimeError("boom")

        def chat(self):
            return self._Chat()

    for i in range(RECENT_KEEP + 12):
        store.save_chat_message(thread_id="t3", role="user", content=f"m{i}", payload=[], events=[])
    summary, recent = compress_history_if_needed(store, _BadLLM(), "t3")
    assert summary == "" and len(recent) == RECENT_KEEP


def test_remember_and_recall_tools(store, llm, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text})
    pid = collector["project_id"]
    collector["conversation_id"] = "conv1"

    r = tools["remember"].invoke({"content": "结尾要克制", "kind": "preference"})
    assert "已记住" in r
    r2 = tools["remember"].invoke({"content": "城市是赛博朋克", "kind": "decision"})
    assert "已记住" in r2
    r3 = tools["recall"].invoke({})
    assert "结尾要克制" in r3 and "赛博朋克" in r3
    # decision 落在项目作用域，别的项目看不见
    assert any(m.scope == "project" and m.project_id == pid
               for m in store.list_memories(project_id=pid, conversation_id="conv1"))
    # 无项目时 decision 拒绝
    collector2 = {"project_id": None, "payloads": [], "conversation_id": None}
    tools2 = {t.name: t for t in build_chat_tools(store, llm, collector2)}
    assert "需要先有项目" in tools2["remember"].invoke({"content": "x", "kind": "decision"})


def test_prepare_turn_injects_memory_and_summary(store, llm, settings, sample_text):
    """一轮对话的上下文要包含记忆小节与项目注记（无 key 环境走 resume 路径验证不到 LLM，直接查 _prepare_turn）。

    消息协议是 OpenAI 兼容 dict——内核（chat/loop.py）用这个协议跟模型对话，
    LangChain 只在调模型那一刻被适配一次。
    """
    from app.chat.conductor import _prepare_turn

    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text})
    pid = collector["project_id"]
    conv = store.ensure_default_conversation(pid)
    store.add_memory(kind="decision", scope="project", project_id=pid, content="风格锁定：雨夜霓虹")
    store.save_chat_message(thread_id=conv.id, role="assistant", content="好的", payload=[], events=[])

    _, loop, messages = _prepare_turn(store, llm, None, conv.id, pid, "继续", None)
    # 消息是 OpenAI 兼容的 dict（见 app/chat/loop.py），不再是 LangChain 消息对象。
    system_text = messages[0]["content"]
    assert "雨夜霓虹" in system_text
    assert "[此前对话摘要]" not in system_text  # 历史很短，不产生摘要
    assert [m["role"] for m in messages][0] == "system"
    assert messages[-1]["role"] == "user" and messages[-1]["content"] == "继续"
    assert loop is not None


# ---------- 长篇小说：诚实化 / 建图 / 选段生成 / 选弧 ----------


def _long_novel() -> str:
    chapters = []
    for i in range(1, 6):
        chapters.append(f"第{'一二三四五'[i - 1]}章 试炼{i}\n" + f"林然在巷口遇上了红雨衣男人，交出了第{i}份情报，雨始终没有停。" * 200)
    return "\n".join(chapters)


class _ArcLLM:
    """回答选弧请求的假模型。"""

    available = True
    calls: list = []

    class _Chat:
        def invoke(self, msgs):
            _ArcLLM.calls.append(msgs)
            return type("R", (), {"content": '[{"title":"巷口对峙","from":1,"to":3,"hook":"情报交易被撞破","why":"冲突密集"}]'})()

    def chat(self):
        return self._Chat()


class _MapLLM:
    """回答章节摘要请求的假模型。"""

    available = True

    class _Chat:
        def invoke(self, msgs):
            return type("R", (), {"content": "剧情：林然交出情报。\n人物：林然、红雨衣男人"})()

    def chat(self):
        return self._Chat()


def test_create_project_warns_on_long_text(store, llm, sample_text):
    tools, collector = _make_tools(store, llm)
    r = tools["create_project"].invoke({
        "title": "长篇", "adaptation_type": "short_drama", "raw_text": _long_novel(),
    })
    assert "万字" in r and "章节地图" in r, r
    # 短文不警告
    r2 = tools["create_project"].invoke({"title": "短篇", "adaptation_type": "short_drama", "raw_text": sample_text})
    assert "章节地图" not in r2


def test_map_novel_tool_builds_map_inline(store):
    from app.chat import build_chat_tools

    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, _MapLLM(), collector)}
    tools["create_project"].invoke({"title": "长篇", "adaptation_type": "short_drama", "raw_text": _long_novel()})
    pid = collector["project_id"]
    r = tools["map_novel"].invoke({"project_id": pid})
    assert "5" in r and "章" in r
    m = store.get_project(pid).novel_map
    assert len(m["chapters"]) == 5 and m["chapters"][0]["summary"] == "林然交出情报。"
    # 已有地图时拒绝重复建图（防止同一请求反复触发全书逐章摘要），rebuild=true 才重建
    r2 = tools["map_novel"].invoke({"project_id": pid})
    assert "已经有章节地图" in r2
    r3 = tools["map_novel"].invoke({"project_id": pid, "rebuild": "true"})
    assert "5" in r3


def test_generate_script_with_span_sets_progress(store, llm, settings, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "连载", "adaptation_type": "short_drama", "raw_text": _long_novel()})
    pid = collector["project_id"]
    store.set_novel_map(pid, {"chapters": [
        {"index": i, "title": f"第{i}章", "summary": f"事件{i}", "characters": [], "chars": 100} for i in range(1, 6)
    ]})
    r = tools["generate_script"].invoke({"project_id": pid, "from_chapter": "2", "to_chapter": "4"})
    assert "第 2–4 章" in r, r
    p = store.get_project(pid)
    assert p.adapted_until == 4
    version = store.latest_version(p)
    assert "第 2–4 章" in (version.label or "")
    # 无模型走本地回退也应有版本产出
    assert version.source_type == "generation"


def test_generate_script_long_text_honest_truncation(store, llm, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "长篇", "adaptation_type": "short_drama", "raw_text": _long_novel()})
    pid = collector["project_id"]
    r = tools["generate_script"].invoke({"project_id": pid})
    assert "只基于开头" in r, r


def test_propose_arcs_tool(store, llm, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "长篇", "adaptation_type": "short_drama", "raw_text": _long_novel()})
    pid = collector["project_id"]
    # 没建图就选弧 → 引导建图
    r0 = tools["propose_arcs"].invoke({"project_id": pid})
    assert "章节地图" in r0
    store.set_novel_map(pid, {"chapters": [
        {"index": i, "title": f"第{i}章", "summary": f"事件{i}", "characters": [], "chars": 100} for i in range(1, 6)
    ]})
    collector_arc = {"project_id": pid, "payloads": []}
    from app.chat import build_chat_tools
    arc_tools = {t.name: t for t in build_chat_tools(store, _ArcLLM(), collector_arc)}
    r = arc_tools["propose_arcs"].invoke({"project_id": pid})
    assert "巷口对峙" in r and "1–3" in r
    # 连载进度后的默认起点
    store.set_adapted_until(pid, 3)
    arc_tools["propose_arcs"].invoke({"project_id": pid})
    user_msg = _ArcLLM.calls[-1][-1]["content"]
    assert "第 4 章" in user_msg


# ---------- 编造动作护栏（deepseek 实测会模仿轨迹格式编结果） ----------


def test_guard_flags_claims_without_tool_results():
    from app.chat.conductor import _guard_unverified_claims

    # 声称生成了版本、但本轮没有任何工具返回 → 追加更正
    fabricated = "已生成初稿版本 ver_ab12cd34ef56（模式：llm）：共 5 场。"
    guarded = _guard_unverified_claims(fabricated, events=[])
    assert "系统更正" in guarded and "不可信" in guarded

    # 提候选故事弧但没有工具返回 → 同样更正
    assert "系统更正" in _guard_unverified_claims("为你挑出 3 个候选故事弧：…", events=[])

    # 有真实工具返回 → 不动正文
    events = [{"type": "tool_result", "name": "generate_script", "summary": "已生成初稿版本 ver_x"}]
    legit = "已生成初稿版本 ver_x：共 3 场。"
    assert _guard_unverified_claims(legit, events) == legit

    # 纯讨论（无结果短语）→ 不动
    chat = "当前版本是 ver_x，你觉得节奏怎么样？"
    assert _guard_unverified_claims(chat, events=[]) == chat


def test_no_add_asset_tool_anymore(store, llm):
    """对话里不再有「登记公网直链」这条路。

    参考视频只能来自参考素材（本地文件），提交给服务商时内联成 data URI；
    公网直链这条路整条拆掉了——服务商拉不到本机回放地址，留着只会让人踩空。
    """
    tools, _ = _make_tools(store, llm)
    assert "add_asset" not in tools


# ---------- 主 Agent 的按需技能加载 ----------

#: 触发器 → 该行必须指向的工具。这张表是「改造时别把某条流程弄丢」的护栏：
#: 提示词从 15 条编号规则改写成触发器表时，最容易的失误就是漏掉一条。
TRIGGERS = [
    ("新建剧本", "create_project"),
    ("生成初稿", "generate_script"),
    ("改编（", "run_adaptation"),
    ("咨询（", "ask"),
    ("拆镜头", "breakdown_scenes"),
    ("风格指南", "generate_style_guide"),
    ("定妆图", "generate_reference_assets"),
    ("视频 prompt", "generate_video_prompts"),
    # 查询类只有一个入口：进度/后台/合成/镜头盘点全部收敛到 get_status。
    ("问进度", "get_status"),
    ("批准提示词", "approve_video_prompts"),
    ("出片（", "submit_videos"),
    ("合成成片", "finalize_film"),
    ("记忆：", "remember"),
]


def test_load_skill_tool_loads_main_skill(store, llm):
    tools, _ = _make_tools(store, llm)
    assert "load_skill" in tools
    body = tools["load_skill"].invoke({"name": "novel-serial"})
    assert "章节地图" in body and "generate_script" in body


def test_load_skill_rejects_crew_skill_and_typos(store, llm):
    """工具报错会污染消息流，所以未知技能返回可读提示 + 可用清单，让模型自己改口。"""
    tools, _ = _make_tools(store, llm)
    for bad in ("director-breakdown", "novel-serail", ""):
        out = tools["load_skill"].invoke({"name": bad})
        assert "没有这个技能" in out
        assert "novel-serial" in out


def test_system_prompt_lists_every_main_skill():
    prompt = system_prompt()
    for name in main_skill_names():
        assert name in prompt, f"技能 {name} 没进系统提示的目录，模型永远不会加载它"
    # 占位符必须被真的替换掉，漏了等于把 {skill_index} 这串字喂给模型
    assert "{skill_index}" not in prompt


def test_system_prompt_keeps_hard_rules():
    """提示词可以重写，铁律不能丢。"""
    prompt = system_prompt()
    for kw in (
        "汇报以工具返回为准",
        "严禁凭对话历史编造版本号",
        "不替用户做最终决定",
        "需要用户先配置自己的 API Key",
    ):
        assert kw in prompt, f"系统提示里丢了「{kw}」"


def test_system_prompt_stays_within_budget():
    """目录只放 name+description，正文靠 load_skill 拉——所以技能变多，提示词也只长几行。"""
    assert len(system_prompt()) < 2400, f"系统提示 {len(system_prompt())} 字，超出预算"


def test_every_trigger_points_at_a_real_tool(store, llm):
    tools, _ = _make_tools(store, llm)
    prompt = system_prompt()
    lines = prompt.splitlines()
    for kw, tool_name in TRIGGERS:
        line = next((ln for ln in lines if kw in ln), "")
        assert line, f"触发器「{kw}」不在系统提示里了"
        assert tool_name in line, f"「{kw}」这条触发器没有指向 {tool_name}"
        assert tool_name in tools, f"提示词点名了不存在的工具 {tool_name}"


def test_main_skills_related_tools_exist(store, llm):
    """技能末尾「相关工具」里点名的东西必须是真实工具，防止代码改名后文档悄悄失真。"""
    import re

    from app.skills import list_skills, load_skill

    tools, _ = _make_tools(store, llm)
    for meta in list_skills("main"):
        tail = load_skill(meta.name).body.split("# 相关工具")[-1]
        named = set(re.findall(r"`([a-z_]+)`", tail))
        assert named, f"{meta.name} 的「相关工具」一节是空的"
        assert named <= set(tools), f"{meta.name} 点名了不存在的工具：{named - set(tools)}"


# ---------- @ 引用资产 ----------


def test_refs_note_lists_mentioned_assets():
    """用户用 @ 引用资产时，本轮消息里要列出「类型 + 编号 + 名字」。

    只写「@林然」三个字，Agent 不知道它是哪部作品里的哪个角色；
    上下文里必须能定位到资产库条目。
    """
    from app.chat.conductor import _refs_note, _user_turn_content

    note = _refs_note([
        {"kind": "character", "id": "char_lin", "name": "林然"},
        {"kind": "location", "id": "loc_street", "name": "滨江路"},
    ])
    assert "@林然" in note and "char_lin" in note and "角色" in note
    assert "@滨江路" in note and "loc_street" in note and "场景" in note

    # 空引用不产生任何附加内容
    assert _refs_note([]) == ""
    assert _refs_note(None) == ""

    # 本轮用户消息 = 原文 + 引用清单
    content = _user_turn_content("把 @林然 的定妆照改一下", None, [
        {"kind": "character", "id": "char_lin", "name": "林然"},
    ])
    assert "把 @林然 的定妆照改一下" in content
    assert "【本轮引用的资产】" in content


def test_system_prompt_teaches_at_semantics():
    """系统提示要教会 Agent @ 的含义，否则它会把 @林然 当成普通文字。"""
    from app.chat.conductor import system_prompt

    sp = system_prompt()
    assert "@" in sp and "引用" in sp
