"""技能（Skill）—— 把长指令外置成 SKILL.md，按需加载。

设计说明见 [`loader.py`](loader.py)；选型理由见 `docs/DECISIONS.md` D-12。

    from app.skills import list_skills, skill_index, load_skill, render_skill

    skill_index()                       # 目录（只有 name + description）
    render_skill("director-breakdown")  # 正文（可带变量）
    skill_file("director-breakdown", "x.md")  # 附属文件（按需）
"""

from __future__ import annotations

from .loader import (
    SKILLS_DIR,
    Skill,
    SkillMeta,
    SkillNotFound,
    clear_cache,
    list_skills,
    load_skill,
    parse_skill_text,
    render,
    render_skill,
    skill_file,
    skill_index,
)

__all__ = [
    "SKILLS_DIR",
    "parse_skill_text",
    "Skill",
    "SkillMeta",
    "SkillNotFound",
    "clear_cache",
    "list_skills",
    "load_skill",
    "render",
    "render_skill",
    "skill_file",
    "skill_index",
]
