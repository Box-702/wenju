# =====================================================================
# test_agent.py —— Agent 端到端测试（无 RAG 版）
#
# 验证最核心的「LangGraph 人机协同」链路。
# =====================================================================

from app.agent import runner as agent_svc
from app.pipeline.generation import generate_script
from app.pipeline.patch import validate_script
from app.store import Store


def _make_project(store: Store, sample_text: str, title: str = "雨夜"):
    p = store.create_project(title=title, adaptation_type="short_drama", language="zh-CN", raw_text=sample_text)
    return p


def test_generate_then_adapt_full_flow(store, llm, settings, sample_text):
    # 【1】生成（无模型 -> 本地回退）。
    p = _make_project(store, sample_text)
    script, artifacts = generate_script(
        llm, settings, title=p.title, raw_text=p.raw_text,
        adaptation_type=p.adaptation_type, language=p.language,
    )
    version = store.create_version(p, script, source_type="generation", label="初始生成")

    # 【2】启动 Agent 运行 -> 应到达 review 中断并返回提议。
    run = agent_svc.start_agent_run(
        store, llm, settings,
        project=p,
        base_version=version,
        instruction="把节奏改紧凑。",
        scene_ids=[],
    )
    assert run["status"] == "reviewing", run
    assert run["patch"], "无模型回退也应产出可审阅的 patch"
    stored = store.get_agent_run(run["run_id"])
    assert stored.status == "reviewing"

    # 【3】接受 -> 生成新版本。
    result = agent_svc.resume_agent_run(
        store, llm, settings,
        run_id=run["run_id"],
        action="accept",
        patch_indexes=None,
    )
    assert result["status"] == "applied", result
    assert result.get("new_version_id"), result
    new_version = store.get_version(result["new_version_id"])
    assert new_version.source_type == "agent_adaptation"
    assert not [i for i in validate_script(new_version.script) if i.severity == "error"]


def test_reject_run_sets_rejected(store, llm, settings, sample_text):
    p = _make_project(store, sample_text)
    script, _ = generate_script(
        llm, settings, title=p.title, raw_text=p.raw_text,
        adaptation_type=p.adaptation_type, language=p.language,
    )
    version = store.create_version(p, script, source_type="generation", label="初始生成")

    run = agent_svc.start_agent_run(
        store, llm, settings,
        project=p, base_version=version, instruction="改一下", scene_ids=[],
    )
    result = agent_svc.resume_agent_run(
        store, llm, settings,
        run_id=run["run_id"],
        action="reject",
        patch_indexes=None,
    )
    assert result["status"] == "rejected", result
    stored = store.get_agent_run(run["run_id"])
    assert stored.status == "rejected"


def _start(store, llm, settings, sample_text):
    p = store.create_project(title="雨夜", adaptation_type="short_drama", language="zh-CN", raw_text=sample_text)
    script, _ = generate_script(
        llm, settings, title=p.title, raw_text=p.raw_text,
        adaptation_type=p.adaptation_type, language=p.language,
    )
    version = store.create_version(p, script, source_type="generation", label="初始生成")
    run = agent_svc.start_agent_run(
        store, llm, settings, project=p, base_version=version,
        instruction="改紧凑一点。", scene_ids=[],
    )
    return p, version, run


def test_edit_run_applies_human_patch(store, llm, settings, sample_text):
    """HITL：人类在中断处「编辑」patch（人工修订标题），应生成新版本。"""
    p, version, run = _start(store, llm, settings, sample_text)
    assert run["status"] == "reviewing"

    edited = [{"op": "set", "path": "/script/scenes/0/title", "value": "雨夜对峙"}]
    result = agent_svc.resume_agent_run(
        store, llm, settings, run_id=run["run_id"], action="edit",
        patch_indexes=None, patch=edited,
    )
    assert result["status"] == "applied", result
    new_version = store.get_version(result["new_version_id"])
    assert new_version.script.scenes[0].title == "雨夜对峙"
    assert new_version.source_type == "agent_adaptation"


def test_regenerate_then_accept(store, llm, settings, sample_text):
    """HITL：先「重新生成」（带反馈）再次中断，再接受生成新版本。"""
    p, version, run = _start(store, llm, settings, sample_text)
    result = agent_svc.resume_agent_run(
        store, llm, settings, run_id=run["run_id"], action="regenerate",
        patch_indexes=None, patch=None, feedback="再口语一点",
    )
    assert result["status"] == "reviewing", result

    final = agent_svc.resume_agent_run(
        store, llm, settings, run_id=run["run_id"], action="accept",
    )
    assert final["status"] == "applied", final
    assert final.get("new_version_id")


def test_旧改编提议不能覆盖更新的剧本(store, llm, settings, sample_text):
    p, base, run = _start(store, llm, settings, sample_text)
    newer = store.create_version(p, base.script, source_type="manual_edit",
                                 label="用户的新版本", parent_version_id=base.id,
                                 set_current=True)
    result = agent_svc.resume_agent_run(
        store, llm, settings, run_id=run["run_id"], action="accept",
    )
    assert result["status"] == "stale"
    assert store.latest_version(store.get_project(p.id)).id == newer.id
    assert store.get_agent_run(run["run_id"]).status == "stale"
