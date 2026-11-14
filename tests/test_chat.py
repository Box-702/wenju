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
    # 统一标签表（context.PROMPT_STATUS_LABELS）：draft 显示「草稿」，
    # 不再与 needs_review（待审）混叫——四态各有其词是这次审计修掉的问题之一。
    assert "shot_scene001_000" in full and "草稿" in full


def test_approve_video_prompts_requests_then_confirms(store, llm, sample_text):
    """批准是两步硬闸门（D-43）：工具只发起请求，用户确认后 approved 才写入。"""
    from app.chat.approvals import apply_approval, detect_confirmation

    tools, collector, pid = _make_video_project(store, llm, sample_text)
    r = tools["approve_video_prompts"].invoke({"project_id": pid, "shot_ids": "shot_scene001_000"})
    assert "已发起批准请求" in r
    # 模型调完工具时状态没变——它不能声称「已批准」。
    versions = sorted(store.list_video_versions(pid), key=lambda v: v.created_at)
    shot = next(s for s in versions[-1].shots if s["id"] == "shot_scene001_000")
    assert shot["prompt_status"] == "draft"
    # 确认卡片带 approval_id 进 payload
    card = [p for p in collector["payloads"] if p.get("type") == "prompt_approval"]
    assert card and card[0]["shot_ids"] == ["shot_scene001_000"]
    approval = store.get_pending_approval(card[0]["approval_id"])
    assert approval is not None and approval.status == "pending"
    # 确定性路径：用户确认后才真批准，生成新版本
    result = apply_approval(store, approval.project_id, approval.version_id, approval.shot_ids)
    assert result["ok"] and result["approved_count"] == 1
    versions = sorted(store.list_video_versions(pid), key=lambda v: v.created_at)
    shot = next(s for s in versions[-1].shots if s["id"] == "shot_scene001_000")
    assert shot["prompt_status"] == "approved"
    # 已批准的再发起一次：无可批准
    r2 = tools["approve_video_prompts"].invoke({"project_id": pid, "shot_ids": "shot_scene001_000"})
    assert "没有可批准" in r2
    # 文本确认检测（保守）：肯定短句算，否定/疑问/长句不算
    assert detect_confirmation("批准")
    assert detect_confirmation("全部批准")
    assert not detect_confirmation("不批准")
    assert not detect_confirmation("哪些没批准？")
    assert not detect_confirmation("先不批准")
    assert not detect_confirmation("等我看完再批准")
    assert not detect_confirmation("如果没什么大问题的话就批准吧，不过我还是想先仔细看看第三镜的提示词到底怎么写的再决定")


def test_approve_requests_replace_previous(store, llm, sample_text):
    """同一会话重复发起：只保留最新一条待确认请求，旧的置 expired。"""
    tools, _, pid = _make_video_project(store, llm, sample_text)
    tools["approve_video_prompts"].invoke({"project_id": pid})
    first = store.latest_pending_approval(project_id=pid, conversation_id="")
    tools["approve_video_prompts"].invoke({"project_id": pid})
    second = store.latest_pending_approval(project_id=pid, conversation_id="")
    assert first is not None and second is not None and first.id != second.id
    assert store.get_pending_approval(first.id).status == "expired"


# ---------- D-43 批准硬闸门：确定性路径全链路（chat_once 入口） ----------


def _make_pending(store, llm, sample_text, conversation_id=""):
    """造项目 + 镜头方案 + 一条待确认批准请求，返回 (pid, approval_id)。"""
    collector = {"project_id": None, "payloads": [], "conversation_id": conversation_id}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    tools["create_project"].invoke({"title": "雨夜", "adaptation_type": "short_drama", "raw_text": sample_text})
    pid = collector["project_id"]
    store.create_video_version(project_id=pid, shots=[
        {"id": "shot_a", "video_prompt": "A man enters.", "prompt_status": "draft"},
    ], source_type="agent", label="导演拆解")
    r = tools["approve_video_prompts"].invoke({"project_id": pid})
    assert "已发起批准请求" in r
    return pid, collector["payloads"][-1]["approval_id"]


def test_approval_confirm_via_meta(store, llm, settings, sample_text):
    """前端卡片「确认批准」→ meta 确定性路径：模型缺席也能批准生效（闸门不依赖 LLM）。"""
    pid, approval_id = _make_pending(store, llm, sample_text)
    out = chat_once(store, llm, settings, conversation_id=None, project_id=pid, message="",
                    meta={"intent": "approve_prompts", "approval_id": approval_id, "action": "confirm"})
    assert "已批准" in out["reply"], out
    assert store.get_pending_approval(approval_id).status == "confirmed"
    v = sorted(store.list_video_versions(pid), key=lambda x: x.created_at)[-1]
    assert next(s for s in v.shots if s["id"] == "shot_a")["prompt_status"] == "approved"


def test_approval_confirm_via_text(store, llm, settings, sample_text):
    """用户回一句「批准」：确定性检测直接生效，不进 LLM（无模型环境也能走通就是证据）。"""
    pid, approval_id = _make_pending(store, llm, sample_text, conversation_id="conv_x")
    out = chat_once(store, llm, settings, conversation_id="conv_x", project_id=pid, message="批准", meta=None)
    assert "已批准" in out["reply"] and "没有配置对话模型" not in out["reply"]
    assert store.get_pending_approval(approval_id).status == "confirmed"

    # 没有待确认请求时，「批准」两个字只是普通聊天（交回 LLM 路径）。
    out2 = chat_once(store, llm, settings, conversation_id="conv_x", project_id=pid, message="批准", meta=None)
    assert "没有配置对话模型" in out2["reply"]


def test_approval_negative_text_not_confirmed(store, llm, settings, sample_text):
    """否定/疑问措辞绝不触发批准：「不批准」只是聊天，请求保持 pending。"""
    pid, approval_id = _make_pending(store, llm, sample_text, conversation_id="conv_y")
    for phrase in ("不批准", "哪些没批准？", "先不批准，等我看看"):
        out = chat_once(store, llm, settings, conversation_id="conv_y", project_id=pid, message=phrase, meta=None)
        assert "没有配置对话模型" in out["reply"], phrase  # 走了 LLM 回退 = 没触发确定性批准
    assert store.get_pending_approval(approval_id).status == "pending"


def test_approval_cancel_via_meta(store, llm, settings, sample_text):
    """卡片「先不批准」：请求终结为 cancelled，镜头状态不变。"""
    pid, approval_id = _make_pending(store, llm, sample_text)
    out = chat_once(store, llm, settings, conversation_id=None, project_id=pid, message="",
                    meta={"intent": "approve_prompts", "approval_id": approval_id, "action": "cancel"})
    assert "已取消" in out["reply"]
    assert store.get_pending_approval(approval_id).status == "cancelled"
    v = sorted(store.list_video_versions(pid), key=lambda x: x.created_at)[-1]
    assert next(s for s in v.shots if s["id"] == "shot_a")["prompt_status"] == "draft"


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
    """get_status 报后台任务且**按项目过滤**（2026-11-11 审计：此前全局取前 3 条，
    A 作品的对话会看到 B 作品的任务名与结果）。"""
    from app.agent.tasks import SubAgentRunner, SubAgentTask
    from app.chat import build_chat_tools

    runner = SubAgentRunner()
    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector, runner=runner)}
    tools["create_project"].invoke({"title": "雨夜", "raw_text": sample_text})
    pid = collector["project_id"]
    runner._tasks["task_x"] = SubAgentTask(
        id="task_x", name="导演场景拆解", status="done",
        result="已拆出 4 个镜头", project_id=pid,
    )
    # 别的项目的任务：不得出现在本项目的 get_status 里
    runner._tasks["task_y"] = SubAgentTask(
        id="task_y", name="别的作品的出片", status="done",
        result="另一个成片", project_id="proj_other",
    )
    r = tools["get_status"].invoke({"project_id": pid})
    assert "导演场景拆解" in r and "已完成" in r and "4 个镜头" in r
    assert "别的作品的出片" not in r


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
    """假 LLM：达到阈值时压缩旧消息，摘要落库，covered_until_id 锚点正确。

    锚点是最后一条已并入摘要的消息 id（2026-11-11 改用 id 锚点替代绝对计数：
    计数版在总条数超 200 后与读库窗口错位，摘要永久停摆、中段历史静默丢失）。
    """
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
    ids: list[str] = []
    for i in range(total):
        ids.append(store.save_chat_message(thread_id=conv.id, role="user", content=f"消息{i}", payload=[], events=[]).id)
    summary, recent = compress_history_if_needed(store, _FakeLLM(), conv.id)
    assert "事实清单" in summary
    assert len(recent) == RECENT_KEEP
    conv = store.get_conversation(conv.id)
    # 覆盖到最后一条 pending（窗口前那段），锚点就是它的 id
    assert conv.summary["covered_until_id"] == ids[total - RECENT_KEEP - 1]
    # 攒不够新内容时沿用旧摘要，不再调 LLM；但覆盖点到窗口之间那几条并进窗口，不暂时失忆
    summary2, recent2 = compress_history_if_needed(store, _FakeLLM(), conv.id)
    assert summary2 == summary
    assert len(recent2) == RECENT_KEEP
    # 再攒 5 条（不足触发阈值）：覆盖点之后、窗口之前的 5 条必须并进返回窗口，
    # 否则它们既不在摘要也不在上下文——「暂时失忆」（旧版行为）。
    for i in range(5):
        store.save_chat_message(thread_id=conv.id, role="user", content=f"补{i}", payload=[], events=[])
    summary3, recent3 = compress_history_if_needed(store, _FakeLLM(), conv.id)
    assert summary3 == summary  # 不重新调 LLM
    assert len(recent3) == RECENT_KEEP + 5
    assert recent3[-1].content == "补4"


def test_compress_summary_advances_beyond_window(store):
    """长对话回归：>200 条后摘要必须**继续前进**，不再停摆丢中段。

    此前 covered_count（绝对计数）撞上 list_chat_messages 的 200 条窗口：
    covered 钳到 200、pending 恒为空切片，200 条窗口之外、摘要之外的历史
    静默蒸发。锚点版把「滚出窗口的覆盖点」退化成窗口内全量重摘（合并式
    摘要天然去重），摘要每轮照常推进。
    """
    from app.memory import RECENT_KEEP, compress_history_if_needed

    class _FakeLLM:
        available = True

        class _Chat:
            def invoke(self, msgs):
                return type("R", (), {"content": "事实清单：持续推进"})()

        def chat(self):
            return self._Chat()

    conv = store.create_conversation(project_id=None, title="超长对话")
    for i in range(230):
        store.save_chat_message(thread_id=conv.id, role="user", content=f"消息{i}——带一点内容让它像真消息", payload=[], events=[])
    s1, _ = compress_history_if_needed(store, _FakeLLM(), conv.id)
    state1 = store.get_conversation(conv.id).summary
    # 再灌 20 条：窗口整体滑动，覆盖点滚出 200 条窗口之外
    for i in range(20):
        store.save_chat_message(thread_id=conv.id, role="user", content=f"新消息{i}", payload=[], events=[])
    s2, _ = compress_history_if_needed(store, _FakeLLM(), conv.id)
    state2 = store.get_conversation(conv.id).summary
    assert "事实清单" in s2
    assert state2.get("covered_until_id") != state1.get("covered_until_id"), "锚点应继续前进（旧版这里永冻）"


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
    ("读作品", "read_script"),
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


# ---------- import_storyboard 校验（D-47 成品分镜直入） ----------


def test_validate_units_单镜与组都收():
    from app.chat.tools.production import validate_storyboard_units

    raw = [
        {"prompt": "P1 果园过肩镜头", "duration_sec": 4},
        {"prompt": "GROUP 排钱三机位", "duration_sec": 9, "shots": [
            {"duration_sec": 4, "shot_type": "medium", "subject": "客人", "action": "排钱"},
            {"duration_sec": 2, "shot_type": "extreme_close_up", "subject": "铜钱"},
            {"duration_sec": 3, "shot_type": "over_shoulder", "subject": "掌柜"},
        ]},
    ]
    units, errors = validate_storyboard_units(raw)
    assert errors == [] and len(units) == 2
    assert units[1]["shots"][2]["shot_type"] == "over_shoulder"


def test_validate_units_坏数据当场拦():
    from app.chat.tools.production import validate_storyboard_units

    bad = [
        {"prompt": "", "duration_sec": 4},                       # 缺 prompt
        {"prompt": "P", "duration_sec": 30},                     # 超一次生成上限
        {"prompt": "P", "duration_sec": 6, "shots": [
            {"duration_sec": 2, "shot_type": "selfie"},          # 景别非法
            {"duration_sec": 2, "shot_type": "wide"},
        ]},                                                      # 机位和=4≠6
    ]
    units, errors = validate_storyboard_units(bad)
    assert len(units) == 0 or len(errors) >= 3
    assert any("prompt" in e for e in errors)
    assert any("区间" in e for e in errors)
    assert any("景别" in e for e in errors)
    assert any("对不上" in e for e in errors)


def test_import_storyboard_归一化与接力(store, llm):
    """外部全局时间线分镜 → 独立单元的确定性归一化（2026-11-12 果园漂移修复）：
    1) 首段逐字不动；2) 后段剥全局时间戳前缀、补全局锚；
    3) 段头带 chain_from + reference_group，批次串行推进时自动接上段成片。"""
    import json as _json
    tools, collector = _make_tools(store, llm)
    units = _json.dumps([
        {"prompt": "果园午后，过肩镜头：前景虚焦男性肩膀，焦点锁在20岁西方少女面部，"
                   "她眼眶泛红，哽咽说：{Am I a machine without feelings?} 停顿吸气。"
                   "黄金侧逆光勾勒发丝轮廓。",
         "duration_sec": 9, "shots": [
             {"duration_sec": 5, "shot_type": "over_shoulder", "subject": "少女", "action": "踱步"},
             {"duration_sec": 4, "shot_type": "close_up", "subject": "少女", "action": "哽咽"}]},
        {"prompt": "At 00:09, cuts to 30岁中世纪绅士正面近景：眉头微蹙、下颌收紧，沉默倾听。",
         "duration_sec": 4},
        {"prompt": "At 00:13, cuts back 少女过肩镜头：情绪转坚定，直视对方缓慢顿挫念台词。",
         "duration_sec": 6, "shots": [
             {"duration_sec": 3, "shot_type": "over_shoulder", "subject": "少女", "action": "质问"},
             {"duration_sec": 3, "shot_type": "close_up", "subject": "少女", "action": "顿挫"}]},
    ], ensure_ascii=False)
    r = tools["import_storyboard"].invoke({"units": units, "title": "果园归一"})
    assert "已导入" in r
    pid = collector["project_id"]
    versions = sorted(store.list_video_versions(pid), key=lambda v: v.created_at)
    shots = versions[-1].shots

    heads = [s for s in shots if s.get("cut_reason") in ("导入段起点", "导入段")]
    assert len(heads) == 3
    # 首段：逐字保留（无 At 前缀、无补锚）
    p1 = heads[0]["group_prompt"] or heads[0]["video_prompt"]
    assert p1.startswith("果园午后") and "At 00:" not in p1
    # 段二：全局时间戳前缀剥掉、全局锚补在前面、内容保留
    p2 = heads[1]["video_prompt"]
    assert not p2.startswith("At 00:09") and "cuts to" not in p2
    assert p2.startswith("果园午后") and "30岁中世纪绅士" in p2
    # 段三：同前；组提示词在组头
    p3 = heads[2]["group_prompt"]
    assert p3.startswith("果园午后") and "少女过肩镜头" in p3
    # 接力：段一头无 chain；段二/三段头链到上一段段头的 order，且共享环境组
    assert heads[0].get("chain_from") is None
    assert heads[1]["chain_from"] == heads[0]["order"] == 0
    assert heads[2]["chain_from"] == heads[1]["order"]
    for h in heads:
        assert h.get("reference_group") == "g_import_scene"


def test_normalize_units_组内时间戳平移():
    """组单元内部的全局时间戳按「本段起点」平移成段内相对值；越界的删写法留语义。"""
    from app.chat.tools.production import normalize_units
    parsed = [
        {"prompt": "酒馆深夜，40岁掌柜擦杯子，灯光昏黄。[Shot 1] 0-3s 中景推近。", "duration_sec": 3},
        {"prompt": "[Shot 1] 0-2s：门被推开。At 00:05.000, cuts to 掌柜抬头特写。", "duration_sec": 5},
    ]
    out = normalize_units(parsed)
    # 第二段起点=3s：At 00:05 → 段内 2s
    assert "At 00:2.000" in out[1]["prompt"]
    # 越界时间戳（不在本段区间）删除写法
    p = normalize_units([
        {"prompt": "酒馆深夜，40岁掌柜擦杯子。", "duration_sec": 3},
        {"prompt": "At 00:99.000, cuts to 空镜。", "duration_sec": 3},
    ])[1]["prompt"]
    assert "99" not in p and "空镜" in p


def test_normalize_units_全局锚不夹带动作指令():
    """锚只传静态事实（环境/机位/人物身份），动作子句截断——果园 2026-11-14
    实测：首句「…焦点锁在少女面部，她缓慢踱步打量对方，眼眶泛红，哽咽说：{…}」
    整句进锚后，每个后续单元重演同一套踱步+推近，成片动作运镜重复三次。"""
    from app.chat.tools.production import normalize_units
    out = normalize_units([
        {"prompt": "果园午后，过肩镜头：前景虚焦男性肩膀，焦点锁在20岁西方少女面部，"
                   "她缓慢踱步打量对方，眼眶泛红，哽咽说：{Am I a machine?} 说完停顿吸气。",
         "duration_sec": 9},
        {"prompt": "At 00:09, cuts to 30岁中世纪绅士正面近景：他沉默倾听。", "duration_sec": 4},
    ])
    u2 = out[1]["prompt"]
    assert "果园午后" in u2 and "少女" in u2          # 静态锚保留
    assert "踱步" not in u2 and "泛红" not in u2       # 动作子句被截断
    assert "Am I a machine" not in u2                 # 台词不进锚
    assert "绅士正面近景" in u2                        # 本段内容原样


def test_import_storyboard_悬空指代闸门端到端(store, llm):
    import json as _json
    tools, collector = _make_tools(store, llm)
    units = _json.dumps([
        {"prompt": "酒馆深夜，40岁掌柜擦杯子。", "duration_sec": 5},
        {"prompt": "At 00:05, cuts to 少女特写：她唱道：{La la} 继续唱后半段。", "duration_sec": 6},
    ], ensure_ascii=False)
    r = tools["import_storyboard"].invoke({"units": units, "title": "闸门测试"})
    assert "悬空指代" in r and "未落库" in r
    assert collector["project_id"] is None  # 拦在落库之前


def test_import_storyboard_台词超载闸门(store, llm):
    import json as _json
    tools, _ = _make_tools(store, llm)
    long_speech = " ".join(["I have as much soul as you"] * 8)
    units = _json.dumps([
        {"prompt": f"果园午后，20岁少女站立，哽咽说：{{{long_speech}}}", "duration_sec": 6},
    ], ensure_ascii=False)
    r = tools["import_storyboard"].invoke({"units": units, "title": "超载测试"})
    assert "台词" in r and "超过" in r


def test_estimate_speech_seconds_中英口径():
    from app.chat.tools.production import estimate_speech_seconds
    # 6 个英文词 ≈ 2.7s；12 个汉字 ≈ 3.2s
    assert 2.0 <= estimate_speech_seconds("{I have as much soul as you}") <= 3.5
    assert 2.5 <= estimate_speech_seconds("「我没有感情没有灵魂吗」") <= 4.5
    assert estimate_speech_seconds("没有台词的镜头") == 0


def test_import_storyboard_台词重播闸门(store, llm):
    """completed beats 禁止重播（借鉴清单 #4）：同一条台词引用在后续单元
    再出现=独立采样会把这句再念一遍，当场拦。"""
    import json as _json
    tools, collector = _make_tools(store, llm)
    units = _json.dumps([
        {"prompt": "酒馆深夜，40岁掌柜擦杯子，对进门者说：{You again? What do you want now?}", "duration_sec": 8},
        {"prompt": "At 00:08, cuts to 来客近景：他听到那句「You again? What do you want now?」，眉头皱起。", "duration_sec": 6},
    ], ensure_ascii=False)
    r = tools["import_storyboard"].invoke({"units": units, "title": "重播测试"})
    assert "重播" in r and "未落库" in r
    assert collector["project_id"] is None


def test_check_units_锚句引用不计重播():
    """normalize 前置的全局锚在台词引用前截断，不会把首段台词带进后段
    造成误报——果园 v3 修正版（台词各段写全文、互不引用）必须放行。"""
    from app.chat.tools.production import check_units_selfcontained
    parsed = [
        {"prompt": "果园午后，20岁西方少女过肩，哽咽说道：{Am I a machine without feelings?}", "duration_sec": 9},
        {"prompt": "果园午后，20岁西方少女过肩，眼眶泛红。30岁绅士正面近景，眉头微蹙沉默倾听。", "duration_sec": 6},
        {"prompt": "果园午后，20岁西方少女过肩。她说道：{I have as much soul as you, and full as much heart!}", "duration_sec": 8},
    ]
    assert check_units_selfcontained(parsed) == []
