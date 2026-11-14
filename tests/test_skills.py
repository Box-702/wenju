# =====================================================================
# test_skills.py —— 技能加载器
#
# 覆盖：frontmatter 解析、渐进披露（目录 vs 正文）、模板渲染（变量/条件块）、
# 缺变量必须报错（防漂移），以及现有技能是否都能正常解析。
# =====================================================================

from __future__ import annotations

import re

import pytest

from app.skills import (
    SKILLS_DIR,
    load_skill,
    list_skills,
    render,
    render_skill,
    skill_index,
)
from app.skills.loader import SkillNotFound


# ---------- 现有技能 ----------


def test_all_bundled_skills_parse():
    """仓库里的每个 SKILL.md 都要能解析；坏一个会让对应角色直接拿不到提示词。"""
    skills = list_skills()
    names = {s.name for s in skills}
    assert {"director-breakdown", "video-prompt-zh", "video-prompt-en", "style-guide"} <= names
    for s in skills:
        assert s.description.strip(), f"{s.name} 缺少 description"
        assert load_skill(s.name).body.strip(), f"{s.name} 正文为空"


def test_skill_index_is_metadata_only():
    """渐进披露第一级：目录只给 name + description，不含正文。"""
    idx = skill_index()
    assert "director-breakdown" in idx
    # 正文里的细节不该出现在目录里
    assert "先戏剧，后语法" not in idx


def test_director_skill_keeps_the_drama_rules():
    """导演技能搬到文件后，那套方法论一条都不能丢。"""
    body = render_skill("director-breakdown")
    for kw in ["先戏剧，后语法", "三工作规则", "细节律", "最终画面", "五锚点",
               "运镜必须有动机", "节奏阶梯", "不豁免", "越轴", "正向描述"]:
        assert kw in body, f"导演技能缺了「{kw}」"


def test_video_prompt_skills_keep_the_rules():
    """摄影技能锁文本（2026-11-12 D-46 修订：十一条→十三条）。
    规则 9 从「台词不进提示词」改为官方音画句式；新增 12 故意不完美、
    13 轴线互斥特征。漏一条 = 一次提交事故，锁死。"""
    zh = render_skill(
        "video-prompt-zh", provider_label="MiniMax", duration_note="6 秒",
        lang_rule="中文", json_note="中文", has_anchors=True,
    )
    for kw in ["虚拟无人机", "呼吸感", "光从哪个方向来", "编上号", "背景人物",
               "只写画面里看得见的东西", "不越轴", "主事件", "禁止否定句",
               "角色说话", "最终画面收尾", "情绪只写身体", "占位词",
               "故意不完美", "互斥"]:
        assert kw in zh, f"中文摄影技能缺了「{kw}」"

    en = render_skill(
        "video-prompt-en", provider_label="Runway", duration_note="10s",
        lang_rule="English", json_note="English", has_anchors=True,
    )
    assert "prompt_body" in en
    for kw in ["Character says", "deliberate imperfection", "mutually exclusive"]:
        assert kw in en, f"英文摄影技能缺了「{kw}」（中英必须同步，D-46）"
    assert len(en) > len(zh)  # 英文正文更长


def test_style_guide_skill_keeps_the_anchor_rules():
    body = render_skill("style-guide", lang_note="中文")
    for kw in ["写实人物公式", "环境会表演", "环境压力", "客观机位", "禁止出现身份与职业词",
               "绝对不要列举具体运镜方式"]:
        assert kw in body, f"美术技能缺了「{kw}」"


# ---------- 模板渲染 ----------


def test_render_replaces_variables():
    out = render("目标：{{provider}}，时长 {{sec}} 秒", {"provider": "MiniMax", "sec": 6})
    assert out == "目标：MiniMax，时长 6 秒"


def test_render_handles_json_single_braces():
    """提示词里全是 JSON 示例——单花括号必须原样保留，不能被当占位符。"""
    text = '输出：{"scenes": [{"scene_id": "scene_001"}]}，模型 {{label}}'
    out = render(text, {"label": "X"})
    assert '{"scenes": [{"scene_id": "scene_001"}]}' in out
    assert "模型 X" in out


def test_render_if_block_true_and_false():
    text = "前{% if has_anchors %}有锚{% else %}无锚{% endif %}后"
    assert render(text, {"has_anchors": True}) == "前有锚后"
    assert render(text, {"has_anchors": False}) == "前无锚后"


def test_render_if_block_without_else():
    text = "前{% if flag %}可见{% endif %}后"
    assert render(text, {"flag": True}) == "前可见后"
    assert render(text, {"flag": False}) == "前后"


def test_render_treats_empty_string_as_false():
    assert render("{% if x %}有{% else %}无{% endif %}", {"x": "  "}) == "无"


def test_render_raises_on_missing_variable():
    """缺变量必须报错，不能静默留一个 {{x}} 在提示词里——那是漂移。"""
    with pytest.raises(KeyError) as exc:
        render("值：{{missing}}", {})
    assert "missing" in str(exc.value)


def test_render_raises_on_missing_condition_variable():
    with pytest.raises(KeyError):
        render("{% if nope %}x{% endif %}", {})


# ---------- 加载器边界 ----------


def test_unknown_skill_raises():
    with pytest.raises(SkillNotFound):
        load_skill("no-such-skill")


def test_path_traversal_is_rejected():
    with pytest.raises(SkillNotFound):
        load_skill("../../etc/passwd")


def test_skill_dir_is_inside_app():
    assert SKILLS_DIR.name == "skills"
    assert SKILLS_DIR.parent.name == "app"


# ---------- 受众分组（crew / main） ----------


def test_audience_splits_crew_and_main():
    """两组技能井水不犯河水：剧组方法论不该进主 Agent 的目录，反之亦然。"""
    crew = {s.name for s in list_skills("crew")}
    main = {s.name for s in list_skills("main")}
    assert crew == {"director-breakdown", "video-prompt-zh", "video-prompt-en", "style-guide"}
    assert main == {
        "novel-serial",
        "crew-pipeline",
        "production-delivery",
        "script-review",
        "asset-generation",
        "blockout",
    }
    assert not (crew & main)


def test_every_skill_declares_a_known_audience():
    for meta in list_skills():
        assert meta.audience in ("crew", "main"), f"{meta.name} 的受众是 {meta.audience!r}"


def test_main_skill_index_lists_only_main_skills():
    idx = skill_index("main")
    for name in (
        "novel-serial",
        "crew-pipeline",
        "production-delivery",
        "script-review",
        "asset-generation",
    ):
        assert name in idx
    # 主 Agent 不执行分镜，把导演方法论列进它的目录只会诱导它越位
    assert "director-breakdown" not in idx


def test_unknown_audience_is_loud(tmp_path, monkeypatch, caplog):
    """audience 写成 mian 会让技能「存在但永远不加载」——必须吵，不能静默。"""
    from app.skills import loader

    d = tmp_path / "typo-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: typo-skill\ndescription: 打错受众\naudience: mian\n---\n正文\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "SKILLS_DIR", tmp_path)
    loader.clear_cache()
    try:
        assert loader.list_skills() == []
        assert "audience" in caplog.text
    finally:
        loader.clear_cache()


# ---------- 主 Agent 技能的内容契约 ----------


def test_main_skills_have_real_detail():
    """主 Agent 技能装的是「步骤顺序 + 硬门槛 + 失败处理」，不能是空壳。"""
    for meta in list_skills("main"):
        body = load_skill(meta.name).body
        assert len(body) >= 500, f"{meta.name} 正文只有 {len(body)} 字，细则太薄"


def test_main_skills_are_not_templates():
    """正文是整段塞回工具返回值的，留着 {{x}} / {% if %} 会原样出现在上下文里。"""
    for meta in list_skills("main"):
        body = load_skill(meta.name).body
        assert "{{" not in body, f"{meta.name} 里有模板变量，但主 Agent 技能不做渲染"
        assert "{%" not in body, f"{meta.name} 里有条件块，但主 Agent 技能不做渲染"


def test_main_skills_end_with_related_tools():
    """每个技能结尾都要有「相关工具」一节，方便对照代码里真实存在的工具。"""
    for meta in list_skills("main"):
        assert "# 相关工具" in load_skill(meta.name).body, f"{meta.name} 缺少「相关工具」一节"


# ---------- 防漂移：技能里提到的工具必须真实存在 ----------
#
# 合并工具那一次（23 → 19，五个查询工具并成 get_status）改了 4 个 SKILL.md 才补干净。
# 这类漏改不会报错：模型读到「先调 list_video_shots 看进度」，那个工具已经不存在，
# 它要么换一个不对的工具、要么干脆凭印象说进度。**技能里的工具名必须和代码对齐。**


def _real_tool_names() -> set[str]:
    """取真实工具名（AST 解析，不依赖运行时装配）。

    工具实现按域拆在 app/chat/tools/（conductor.py 只剩编排），全目录扫一遍；
    conductor.py 也扫——它应该已经没有 @tool 了，留着扫描是为了防止有人
    把工具悄悄加回去却绕过这套对齐检查。
    """
    import ast
    from pathlib import Path

    files = list(Path("app/chat/tools").glob("*.py")) + [Path("app/chat/conductor.py")]
    names: set[str] = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(target, ast.Name) and target.id == "tool":
                    names.add(node.name)
    return names


def test_工具清单解析得到预期数量():
    """解析器自身要有效：取不到工具名时，下面的对齐检查会变成永远通过的假测试。"""
    names = _real_tool_names()
    assert len(names) >= 15, f"只解析到 {len(names)} 个工具，AST 解析可能失效了"
    assert "get_status" in names and "breakdown_scenes" in names


def test_skill_tool_refs_exist():
    """「相关工具」里点名的每个工具都必须真实存在。"""
    real = _real_tool_names()
    for meta in list_skills("main"):
        body = load_skill(meta.name).body
        section = body.split("# 相关工具", 1)[1]
        mentioned = set(re.findall(r"`([a-z][a-z0-9_]*)`", section))
        assert mentioned, f"{meta.name} 的「相关工具」一节没点名任何工具"
        unknown = mentioned - real
        assert not unknown, f"{meta.name} 提到不存在的工具：{sorted(unknown)}"


def test_no_retired_tool_names():
    """已合并 / 删除的工具名不许再出现在任何技能里（含 crew 技能）。"""
    retired = {
        "list_video_shots", "get_video_jobs", "check_finalize",
        "check_background_tasks", "get_script_overview",
    }
    for meta in list_skills():
        body = load_skill(meta.name).body
        leftover = sorted(n for n in retired if n in body)
        assert not leftover, f"{meta.name} 仍在提已废弃的工具：{leftover}"
