# =====================================================================
# test_code_holes.py —— 审计发现的「代码空洞」回归锁（2026-11-15）
#
# 这组用例锁的是几处「看着有、其实是空壳」的地方：
#   1. 转场拼接：fade/dissolve 分支此前产出的 filter 与 cut 一模一样
#      （注释承诺的「全局 fade」根本没加），crossfade_duration 传进来没用；
#   2. 取消任务：MiniMax V2 没有取消端点，此前 cancel 恒返回 False 而
#      调用方不知道「远端没停、费用照计」；
#   3. 技能第三级披露：工具告诉模型「该技能还有附属文件」，却没有任何路
#      径把附件读回来——对模型的虚假承诺；
#   4. 生产状态接口忽略 project_id，A 作品读到 B 作品的生产结果。
# =====================================================================


# ---------- 1. 转场拼接 ----------


def _capture_assemble_cmd(monkeypatch, durations, transition="fade", crossfade=0.5):
    """跑一次 _filter_complex，把真正下给 ffmpeg 的命令抓出来。"""
    import app.video.assemble as asm

    monkeypatch.setattr(asm, "_probe_duration", lambda p: durations.get(p, 0.0))
    captured: dict = {}

    class _Result:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(asm.subprocess, "run", _fake_run)
    asm._filter_complex(
        "ffmpeg", ["a.mp4", "b.mp4"], "out.mp4", transition, crossfade,
    )
    return captured["cmd"]


def test_fade_走真转场_xfade链(monkeypatch):
    """fade 分支必须产出 xfade/acrossfade 链，而不是与 cut 相同的纯 concat。"""
    cmd = _capture_assemble_cmd(monkeypatch, {"a.mp4": 4.0, "b.mp4": 3.0})
    filter_str = cmd[cmd.index("-filter_complex") + 1]
    assert "xfade" in filter_str, f"转场分支没走 xfade：{filter_str}"
    assert "acrossfade" in filter_str, f"音频没走 acrossfade：{filter_str}"
    assert "duration=0.500" in filter_str
    assert "concat=" not in filter_str, "转场分支不该再用纯 concat"


def test_dissolve_用对应的xfade转场名(monkeypatch):
    cmd = _capture_assemble_cmd(monkeypatch, {"a.mp4": 4.0, "b.mp4": 3.0})
    filter_str = cmd[cmd.index("-filter_complex") + 1]
    assert "transition=dissolve" in filter_str or "transition=fade" in filter_str


def test_cut_仍是纯concat(monkeypatch):
    """cut 是生产路径的默认值，不能被转场改动带歪。"""
    cmd = _capture_assemble_cmd(monkeypatch, {"a.mp4": 4.0, "b.mp4": 3.0}, transition="cut")
    filter_str = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2" in filter_str
    assert "xfade" not in filter_str


def test_时长探不到时退回硬拼(monkeypatch):
    """探不到时长就不能算 xfade 的 offset——宁可没有转场，也不要拼出黑屏。"""
    cmd = _capture_assemble_cmd(monkeypatch, {"a.mp4": 0.0, "b.mp4": 0.0})
    filter_str = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2" in filter_str
    assert "xfade" not in filter_str


# ---------- 2. 取消任务 ----------


def test_minimax_声明不支持远端取消():
    from app.video.providers.minimax import MiniMaxProvider

    assert MiniMaxProvider.supports_cancel is False


def test_seedance_声明支持取消():
    from app.video.providers.seedance import SeedanceProvider

    assert SeedanceProvider.supports_cancel is True


def test_cancel_job_如实回报远端未取消(store, llm, monkeypatch):
    """MiniMax 没有取消端点：本地置为已取消，但必须说清「远端可能还在跑」。"""
    import app.deps as app_deps
    import app.video.service as video_service

    monkeypatch.setattr(app_deps, "store", lambda: store)
    monkeypatch.setattr(app_deps, "video_manager", lambda: _StubManager())

    project = store.create_project(
        title="雨夜", raw_text="原文", adaptation_type="short_drama", language="zh-CN",
    )
    job = store.create_video_job(
        project_id=project.id, shot_id="shot_a", provider="minimax",
        model="default", prompt="P", params={},
    )
    out = video_service.cancel_video_job(job.id)
    assert out["status"] == "cancelled"
    assert out["remote_cancelled"] is False
    assert out["supports_cancel"] is False
    assert "费用照计" in out["note"], "必须告诉用户远端可能仍在生成、费用照计"
    assert store.get_video_job(job.id).status == "cancelled"


class _StubManager:
    """视频任务管理器打桩：模拟「任务不在内存队列里」（进程重启过）。"""

    def cancel(self, job_id, provider):
        return False


# ---------- 3. 技能第三级披露 ----------


def test_load_skill_能读回附属文件(store, llm, tmp_path, monkeypatch):
    """技能说「还有附件」时，工具必须给模型把附件读回来的路。"""
    from app.chat import build_chat_tools

    skills_dir = tmp_path / "skills" / "demo-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示技能\nwhen_to_use: 测试\n"
        "audience: main\n---\n\n正文。\n\n（该技能还有附属文件：清单.md）\n",
        encoding="utf-8",
    )
    (skills_dir / "清单.md").write_text("附件正文", encoding="utf-8")

    import app.skills.loader as loader

    monkeypatch.setattr(loader, "SKILLS_DIR", tmp_path / "skills")
    loader._all_skills.cache_clear()

    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    # 主技能名列表是缓存的，注入的新技能要能出现在里面
    monkeypatch.setattr(
        "app.chat.tools.memory_tools.main_skill_names",
        lambda: sorted(loader._all_skills_with_db()),
    )

    body = tools["load_skill"].invoke({"name": "demo-skill"})
    assert "正文" in body
    assert "清单.md" in body, "有附件时应该告诉模型附件叫什么"

    attached = tools["load_skill"].invoke({"name": "demo-skill", "file": "清单.md"})
    assert attached.strip() == "附件正文", "附属文件要能被读回来"

    loader._all_skills.cache_clear()


def test_load_skill_没有附件时不空许诺(store, llm):
    """没有附属文件就别提——提了却读不到，是对模型的虚假承诺。"""
    from app.chat import build_chat_tools

    collector = {"project_id": None, "payloads": []}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector)}
    body = tools["load_skill"].invoke({"name": "production-delivery"})
    assert "附属文件" not in body


# ---------- 4. 生产状态按作品过滤 ----------


def test_production_status_按项目过滤(store, llm, settings, monkeypatch):
    """A 作品不该读到 B 作品的生产流水线结果。"""
    from fastapi.testclient import TestClient

    import app.api as api_mod
    from app.agent.tasks import SubAgentRunner, SubAgentTask
    from app.main import create_app

    runner = SubAgentRunner()
    runner._tasks["task_a"] = SubAgentTask(
        id="task_a", name="生产流水线（小说→提示词）", status="done",
        result="A 作品的结果", project_id="proj_a",
    )
    runner._tasks["task_b"] = SubAgentTask(
        id="task_b", name="生产流水线（小说→提示词）", status="done",
        result="B 作品的结果", project_id="proj_b",
    )
    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    monkeypatch.setattr(api_mod.deps, "subagent_runner", lambda: runner)
    client = TestClient(create_app())

    a = client.get("/api/projects/proj_a/production").json()
    assert a["result"] == "A 作品的结果"

    none = client.get("/api/projects/proj_c/production").json()
    assert none["status"] == "idle" and none["task_id"] is None


# ---------- 5. 死代码不许复活 ----------


def test_死代码已清理():
    """这几处曾是零调用的死实现；留着会让「有没有人在用」永远说不清。"""
    import app.agent.runner as runner_mod
    import app.agent.state as state_mod
    import app.chat.conductor as conductor_mod
    import app.deps as deps_mod
    import app.llm.base as llm_base
    import app.llm.image as llm_image
    import app.media.casting as casting_mod
    import app.store as store_mod
    import app.video.base as video_base
    import app.video.finalize as finalize_mod
    import app.video.registry as video_registry

    assert not hasattr(state_mod, "HumanDecision")
    assert not hasattr(runner_mod, "_interrupt_value")
    assert not hasattr(conductor_mod, "thread_is_busy")
    assert not hasattr(deps_mod, "video_registry")
    assert not hasattr(llm_base, "Modality")
    assert not hasattr(llm_image, "register_image_client")
    assert not hasattr(llm_image, "data_uri")
    assert not hasattr(casting_mod, "default_voice_for")
    assert not hasattr(video_registry.ProviderRegistry, "list_available")
    assert not hasattr(finalize_mod, "FinalizeOptions")
    assert not hasattr(store_mod.Store, "list_chat_images")
    assert not hasattr(store_mod.Store, "set_voice_enabled")
    assert not hasattr(video_base.VideoProvider, "estimate_cost")


def test_包路径不再整包再导出():
    """六个包的 __init__ 曾把子模块的公开面整包再导出一遍（40+ 个名字没人用）。"""
    import app.chat as chat_pkg
    import app.crew as crew_pkg
    import app.llm as llm_pkg
    import app.media as media_pkg
    import app.skills as skills_pkg
    import app.video as video_pkg

    assert not hasattr(chat_pkg, "AgentLoop")
    assert not hasattr(crew_pkg, "run_director")
    assert not hasattr(llm_pkg, "Modality")
    assert not hasattr(media_pkg, "cast_script")
    assert not hasattr(skills_pkg, "SkillNotFound")
    assert not hasattr(video_pkg, "VideoProvider")
