# =====================================================================
# test_chat_tools_extra.py —— 对话工具补测（2026-11-11 审计缺口）
#
# 审计发现的零测试工具与新工具在这里补齐：
#   read_source / search_source（原文域此前零测试）
#   clone_voice 归属校验（跨项目素材必须被拒）
#   edit_shot_prompt / cancel_video_job / list_assets / forget（新四件）
#   remember 非法 kind 报错不降级
#   generate_reference_assets 的 entities 范围过滤与深合并
# =====================================================================

from __future__ import annotations

import pytest

from app.chat import build_chat_tools


def _make_tools(store, llm, project_id=None):
    collector = {"project_id": project_id, "payloads": [], "conversation_id": "conv_t"}
    tools = {t.name: t for t in build_chat_tools(store, llm, collector, project_id=project_id)}
    return tools, collector


def _project_with_script(store, llm, sample_text, title="雨夜"):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": title, "raw_text": sample_text})
    pid = collector["project_id"]
    tools["generate_script"].invoke({"project_id": pid})
    return tools, collector, pid


# ---------- 原文域：read_source / search_source ----------


def test_read_source_paginates(store, llm, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "raw_text": sample_text})
    pid = collector["project_id"]

    out = tools["read_source"].invoke({"project_id": pid, "start": 0, "chars": 200})
    assert "原文共" in out and sample_text[:20] in out
    assert "继续读请把 start 设为" in out

    # 越过末尾：如实说到末尾，不编内容
    out2 = tools["read_source"].invoke({"project_id": pid, "start": len(sample_text) + 10})
    assert "已到末尾" in out2


def test_read_source_falls_back_to_current_project(store, llm, sample_text):
    """project_id 缺省时用本轮对话所属作品（模型常只给关键词漏 id）。"""
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "raw_text": sample_text})
    pid = collector["project_id"]

    tools2, _ = _make_tools(store, llm, project_id=pid)
    out = tools2["read_source"].invoke({})
    assert "原文共" in out


def test_read_source_without_any_project(store, llm):
    """无项目上下文时抛 ValueError——AgentLoop._invoke_tool 会把它包成文本回给模型。"""
    tools, _ = _make_tools(store, llm)
    with pytest.raises(ValueError, match="不知道要看哪部作品"):
        tools["read_source"].invoke({})


def test_search_source_finds_and_misses(store, llm, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "raw_text": sample_text})
    pid = collector["project_id"]

    out = tools["search_source"].invoke({"project_id": pid, "keyword": "红雨衣"})
    assert "命中" in out and "红雨衣" in out

    out2 = tools["search_source"].invoke({"project_id": pid, "keyword": "不存在的词"})
    assert "没有出现" in out2

    out3 = tools["search_source"].invoke({"project_id": pid, "keyword": ""})
    assert "给个关键词" in out3


# ---------- clone_voice：归属与类型校验（不碰网络，校验在克隆前） ----------


def test_clone_voice_rejects_foreign_asset(store, llm, sample_text):
    """跨项目素材必须被拒：B 作品的音频不能给 A 的角色克隆音色。"""
    tools, _, pid_a = _project_with_script(store, llm, sample_text, title="雨夜")
    tools2, _, pid_b = _project_with_script(store, llm, sample_text, title="别的项目")

    asset = store.add_asset(pid_b, kind="audio", name="sample.mp3", source="upload",
                            url="/api/assets/file/x", id="asset_x", content=b"fakeaudio")
    out = tools["clone_voice"].invoke({
        "project_id": pid_a, "character": "林然", "sample_asset_id": asset.id,
    })
    assert "不属于本项目" in out


def test_clone_voice_rejects_non_audio_and_missing(store, llm, sample_text):
    tools, _, pid = _project_with_script(store, llm, sample_text)

    out = tools["clone_voice"].invoke({
        "project_id": pid, "character": "林然", "sample_asset_id": "asset_none",
    })
    assert "找不到素材" in out and "list_assets" in out

    img = store.add_asset(pid, kind="image", name="p.png", source="upload",
                          url="/api/assets/file/y", id="asset_y", content=b"png")
    out2 = tools["clone_voice"].invoke({
        "project_id": pid, "character": "林然", "sample_asset_id": img.id,
    })
    assert "不是可用的音频样本" in out2


# ---------- 新工具：edit_shot_prompt / cancel_video_job / list_assets / forget ----------


def _project_with_shots(store, llm, sample_text):
    tools, collector, pid = _project_with_script(store, llm, sample_text)
    store.create_video_version(project_id=pid, shots=[
        {"id": "shot_a", "video_prompt": "A man enters.", "prompt_status": "approved"},
        {"id": "shot_b", "video_prompt": "Rain falls.", "prompt_status": "needs_review"},
    ], source_type="agent", label="测试方案")
    return tools, collector, pid


def test_edit_shot_prompt_resets_approval(store, llm, sample_text):
    """编辑使批准失效：approved → needs_review，且生成新版本（可回滚）。"""
    tools, _, pid = _project_with_shots(store, llm, sample_text)
    out = tools["edit_shot_prompt"].invoke({
        "project_id": pid, "shot_id": "shot_a", "prompt": "A man enters slowly, camera low.",
    })
    assert "已更新镜头 shot_a" in out and "待审" in out
    versions = sorted(store.list_video_versions(pid), key=lambda v: v.created_at)
    shot = next(s for s in versions[-1].shots if s["id"] == "shot_a")
    assert shot["prompt_status"] == "needs_review"
    assert shot["video_prompt"].startswith("A man enters slowly")

    # 未知镜头 / 空 prompt 如实拒绝
    assert "没有找到镜头" in tools["edit_shot_prompt"].invoke(
        {"project_id": pid, "shot_id": "shot_x", "prompt": "x"})
    assert "不能为空" in tools["edit_shot_prompt"].invoke(
        {"project_id": pid, "shot_id": "shot_a", "prompt": "  "})


def test_cancel_video_job_paths(store, llm, sample_text, monkeypatch):
    tools, _, pid = _project_with_shots(store, llm, sample_text)

    # 取消的业务体在 app/video/service.py，它直接读 app.deps 单例——
    # 不重定向就会去连开发库（与三个 REST 测试文件 fixture 同款处理）。
    import app.deps as app_deps
    monkeypatch.setattr(app_deps, "store", lambda: store)

    # 没有任务
    out = tools["cancel_video_job"].invoke({"project_id": pid, "shot_id": "shot_a"})
    assert "没有提交过视频任务" in out

    # 终态任务：不用取消
    job = store.create_video_job(project_id=pid, shot_id="shot_a", provider="minimax",
                                 model="default", prompt="A man enters.", params={})
    store.update_video_job(job.id, status="failed", error_message="boom")
    out2 = tools["cancel_video_job"].invoke({"project_id": pid, "shot_id": "shot_a"})
    assert "已是终态" in out2

    # 在途任务：走 service 取消（provider 解析被 monkeypatch 掉，不碰网络）
    job2 = store.create_video_job(project_id=pid, shot_id="shot_b", provider="minimax",
                                  model="default", prompt="Rain falls.", params={})
    import app.video.service as video_service
    monkeypatch.setattr(video_service, "_resolve_video_provider", lambda name: None)
    out3 = tools["cancel_video_job"].invoke({"project_id": pid, "shot_id": "shot_b"})
    assert "已取消镜头 shot_b" in out3
    assert store.get_video_job(job2.id).status == "cancelled"


def test_list_assets_lists_all_kinds(store, llm, sample_text):
    tools, _, pid = _project_with_shots(store, llm, sample_text)
    store.add_asset(pid, kind="audio", name="sample.mp3", source="upload",
                    url="/api/assets/file/a", id="asset_a", content=b"mp3bytes")
    store.add_asset(pid, kind="video", name="ref.mp4", source="upload",
                    url="/api/assets/file/v", id="asset_v", content=b"mp4bytes")
    store.merge_video_style(pid, {"reference_images": {"char_lin": "http://img/1.png"}})

    out = tools["list_assets"].invoke({"project_id": pid})
    assert "asset_a" in out and "sample.mp3" in out
    assert "asset_v" in out and "ref.mp4" in out
    assert "定妆图" in out

    out_audio = tools["list_assets"].invoke({"project_id": pid, "kind": "audio"})
    assert "asset_a" in out_audio and "asset_v" not in out_audio


def test_forget_deletes_memory_and_rejects_unknown(store, llm, sample_text):
    tools, collector = _make_tools(store, llm)
    tools["create_project"].invoke({"title": "雨夜", "raw_text": sample_text})
    collector["project_id"] = collector["project_id"]

    r = tools["remember"].invoke({"content": "喜欢冷色调", "kind": "preference"})
    assert "已记住" in r
    recall = tools["recall"].invoke({})
    assert "[mem_" in recall  # recall 输出带 id，forget 才有把手

    mid = recall.split("[")[1].split("]")[0]
    out = tools["forget"].invoke({"memory_id": mid})
    assert "已删除记忆" in out
    assert "冷色调" not in tools["recall"].invoke({})

    assert "没有找到记忆" in tools["forget"].invoke({"memory_id": "mem_none"})
    assert "不能为空" in tools["forget"].invoke({"memory_id": " "})


def test_remember_rejects_invalid_kind(store, llm):
    """非法 kind 报错不静默降级：feedback 悄悄变 preference 会把作用域放大到全局。"""
    tools, _ = _make_tools(store, llm)
    out = tools["remember"].invoke({"content": "随便记点", "kind": "wish"})
    assert "kind 只能是" in out
    assert tools["recall"].invoke({}).startswith("还没有任何记忆")


# ---------- generate_reference_assets：entities 范围过滤与深合并 ----------


class _FakeImageClient:
    available = True

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt, negative_prompt=""):
        self.prompts.append(prompt)
        return f"http://img/{len(self.prompts)}.png"


class _FakeVision:
    available = False  # 跳过质检分支


def test_build_reference_assets_entities_scope_and_merge(store, llm, sample_text):
    """只生成点名的实体；写回按实体深合并，未点名的旧图原样保留。"""
    from app.media.refs import build_reference_assets

    tools, _, pid = _project_with_shots(store, llm, sample_text)
    version = store.latest_version(store.get_project(pid))
    script = version.script
    assert script.characters, "测试前提：剧本里要有角色"
    char = script.characters[0]

    # 预置一张「别人的旧图」：范围生成后必须还在
    store.merge_video_style(pid, {"reference_images": {"char_old": "http://img/old.png"}})

    fake = _FakeImageClient()
    refs = build_reference_assets(script, None, image_client=fake, vision_client=_FakeVision(),
                                  entities=[char.name])
    assert list(refs) == [char.id], "只该生成点名的那个角色"
    assert len(fake.prompts) == 1

    # 深合并语义（crew 工具 _run 里的写法）
    current = store.get_video_style(pid) or {}
    merged = {**(current.get("reference_images") or {}), **refs}
    store.merge_video_style(pid, {"reference_images": merged})
    final = (store.get_video_style(pid) or {}).get("reference_images")
    assert final["char_old"] == "http://img/old.png", "未点名的旧图不能丢"
    assert char.id in final


def test_build_reference_assets_all_names_miss_generates_nothing(store, llm, sample_text):
    """点名的名字全对不上：什么都不生成（绝不回退成全量烧钱）。"""
    from app.media.refs import build_reference_assets

    tools, _, pid = _project_with_shots(store, llm, sample_text)
    script = store.latest_version(store.get_project(pid)).script
    fake = _FakeImageClient()
    refs = build_reference_assets(script, None, image_client=fake, vision_client=_FakeVision(),
                                  entities=["根本不存在的人"])
    assert refs == {}
    assert fake.prompts == [], "一个名字都没对上时不该生成任何图"
