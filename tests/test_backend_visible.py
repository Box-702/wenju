# =====================================================================
# test_backend_visible.py —— 「后端有、界面上看不到」的那批能力的接口锁
#
# 审计发现一批能力后端早就做好了，但界面上一个入口都没有：
#   1. 记忆（remember / forget 只在对话工具里，用户看不到自己被记住了什么）；
#   2. 白模施工单与渲染状态（D-54 的三个工具只在对话里）；
#   3. 风格指南正文（色板 / 光线 / 运镜 / 逐字锚，只在库里）；
#   4. 技能来源（哪些是用户导入的、可以删）。
# 这组用例锁住对应接口的形状——界面照着它们写。
# =====================================================================

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api as api_mod
from app.agent.tasks import SubAgentRunner
from app.main import create_app


@pytest.fixture()
def client(store, llm, settings, monkeypatch):
    import app.deps as app_deps

    monkeypatch.setattr(api_mod.deps, "store", lambda: store)
    monkeypatch.setattr(api_mod.deps, "llm", lambda: llm)
    monkeypatch.setattr(api_mod.deps, "settings", lambda: settings)
    monkeypatch.setattr(
        api_mod.deps, "subagent_runner", lambda: SubAgentRunner(storage=store)
    )
    # 技能加载器（app/skills/loader.py）直接读 app.deps.store()——那是全项目
    # 单例，不替换的话用户导入的技能会从真库里读，测试永远读不到自己刚写的。
    monkeypatch.setattr(app_deps, "store", lambda: store)
    return TestClient(create_app())


def _project(client, sample_text):
    resp = client.post("/api/projects/import", data={"raw_text": sample_text, "title": "雨夜"})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 记忆 ----------


def test_memories_列出可停用可删除(client, store, sample_text):
    _project(client, sample_text)
    mem = store.add_memory(
        kind="preference", scope="global", content="喜欢克制的结尾",
    )
    r = client.get("/api/memories")
    assert r.status_code == 200, r.text
    ids = [m["id"] for m in r.json()["memories"]]
    assert mem.id in ids
    item = next(m for m in r.json()["memories"] if m["id"] == mem.id)
    assert item["content"] == "喜欢克制的结尾"
    assert item["scope"] == "global"
    assert item["active"] is True

    # 停用后默认列表不再给（注入提示词时也不看停用的）
    off = client.put(f"/api/memories/{mem.id}?active=false")
    assert off.status_code == 200, off.text
    assert off.json()["active"] is False
    assert mem.id not in [m["id"] for m in client.get("/api/memories").json()["memories"]]
    # 但管理页要看得见停用过的
    allm = client.get("/api/memories?include_inactive=true").json()["memories"]
    assert mem.id in [m["id"] for m in allm]

    # 删除
    d = client.delete(f"/api/memories/{mem.id}")
    assert d.status_code == 200, d.text
    assert client.delete(f"/api/memories/{mem.id}").status_code == 404


def test_memories_按项目过滤(client, store, sample_text):
    project = _project(client, sample_text)
    store.add_memory(
        kind="decision", scope="project", project_id=project["id"], content="本项目：雨夜",
    )
    store.add_memory(kind="decision", scope="project", project_id="other", content="别的项目")
    r = client.get(f"/api/memories?project_id={project['id']}").json()
    contents = [m["content"] for m in r["memories"]]
    assert "本项目：雨夜" in contents
    assert "别的项目" not in contents


# ---------- 白模 ----------


def test_blockout_返回施工单与渲染状态(client, sample_text):
    project = _project(client, sample_text)
    r = client.get(f"/api/projects/{project['id']}/blockout")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_sheet"] is False, "还没起草施工单时不该说有"
    assert body["sheet"] == ""
    assert body["render"]["status"] == "idle"


def test_blockout_起草后能看到施工单原文(client, sample_text, tmp_path, monkeypatch):
    project = _project(client, sample_text)
    # 打的是 API 模块里已经 import 好的那个名字（from ..media.blockout import
    # blockout_dir），改源模块的绑定对它没有影响。
    monkeypatch.setattr(
        "app.api.blockout.blockout_dir", lambda pid: tmp_path / "blender" / pid
    )
    d = tmp_path / "blender" / project["id"]
    d.mkdir(parents=True)
    (d / "施工单.md").write_text(
        "---\nlabel: 雨夜\nfps: 24\nresolution: 960x540\nduration_sec: 3\n"
        "engine: workbench\n---\n\n## 对象\n- 桌 = box,0,0,0,1,1,0.5\n\n"
        "## 镜头\n- 机位1 = wide,0,2,4,1.5,2,0,0,3\n",
        encoding="utf-8",
    )
    r = client.get(f"/api/projects/{project['id']}/blockout")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_sheet"] is True
    assert "机位1" in body["sheet"]
    assert body["render"]["status"] == "idle"


# ---------- 风格指南 ----------


def test_library_返回风格指南正文(client, store, sample_text):
    project = _project(client, sample_text)
    store.set_video_style(project["id"], {
        "visual_style": "anime",
        "color_palette": ["#1a1a2e", "#e94560"],
        "lighting_style": "冷调侧逆光",
        "camera_style": "手持，浅景深",
        "character_appearances": {"char_001": "短发，灰色风衣"},
        "environment_descriptions": {"loc_001": "雨夜的滨江路，路灯昏黄"},
    })
    r = client.get(f"/api/projects/{project['id']}/library")
    assert r.status_code == 200, r.text
    sg = r.json()["style_guide"]
    assert sg["visual_style"] == "anime"
    assert sg["color_palette"] == ["#1a1a2e", "#e94560"]
    assert sg["lighting_style"] == "冷调侧逆光"
    assert sg["camera_style"] == "手持，浅景深"
    assert sg["character_appearances"]["char_001"] == "短发，灰色风衣"
    assert sg["environment_descriptions"]["loc_001"] == "雨夜的滨江路，路灯昏黄"


def test_library_没生成风格指南时给空结构(client, sample_text):
    project = _project(client, sample_text)
    sg = client.get(f"/api/projects/{project['id']}/library").json()["style_guide"]
    assert sg["color_palette"] == []
    assert sg["character_appearances"] == {}


# ---------- 技能来源 ----------


# audience 取自 frontmatter（加载时重新解析正文，不看库里那一列）
SKILL_MD = (
    "---\nname: my-skill\ndescription: 我自己写的\nwhen_to_use: 随便\n"
    "audience: main\n---\n正文"
)


def test_skills_标出用户导入的(client, store):
    store.save_user_skill(
        name="my-skill", description="我自己写的", when_to_use="随便",
        audience="main", content=SKILL_MD,
    )
    skills = client.get("/api/skills").json()["skills"]
    mine = next(s for s in skills if s["name"] == "my-skill")
    builtin = next(s for s in skills if s["name"] == "production-delivery")
    assert mine["user"] is True, "用户导入的技能要标出来（界面上才有删除按钮）"
    assert builtin["user"] is False, "内置技能不可删，不能标成用户导入"


def test_skills_删除用户技能(client, store):
    store.save_user_skill(
        name="my-skill", description="我自己写的", when_to_use="随便",
        audience="main", content=SKILL_MD,
    )
    assert client.delete("/api/skills/my-skill").status_code == 200
    assert client.delete("/api/skills/my-skill").status_code == 404
    names = [s["name"] for s in client.get("/api/skills").json()["skills"]]
    assert "my-skill" not in names
