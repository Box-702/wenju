# =====================================================================
# loader.py —— 技能（Skill）加载器：渐进式披露 + 轻量模板
#
# 背景：剧组三个角色的系统提示词加起来超过 1.5 万字符，全部硬编码在 .py 里。
#   - 改一条规则要改代码（非工程师改不了）；
#   - 改了什么、为什么改，git diff 里混在代码里看不出来；
#   - 一个 skill 无法单独评测、单独替换。
#
# 解法：把「长指令」外置成 SKILL.md（Anthropic Agent Skills 的开放格式），
# 代码只留装配逻辑。
#
# 渐进披露（progressive disclosure）三级：
#   ① 元数据 —— frontmatter 的 name/description，进目录（`skill_index()`）；
#   ② 正文   —— SKILL.md 的 markdown 正文，需要时才加载；
#   ③ 附属文件 —— 同目录下的其他文件，按需读取（`skill_file()`）。
#
# 受众（frontmatter 的 audience）：
#   crew —— 剧组的三个角色，提示词由代码按名加载，人看不到目录；
#   main —— 主对话 Agent，目录进系统提示词，正文靠 `load_skill` 工具按需拉。
#   写错受众会「静默永不加载」，所以非法值直接报错（见 `_parse`）。
#
# 模板非常克制（只支持两种语法，够用即可）：
#   {{var}}                      —— 变量替换
#   {% if var %}…{% else %}…{% endif %} —— 条件块（不支持嵌套）
#
# **为什么不用 str.format()**：提示词里全是 JSON 示例（`{"scene_id": ...}`），
# format 会把所有单花括号当占位符，直接炸。只认双花括号最安全。
# =====================================================================

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

#: SKILL.md 所在目录（app/skills/）
SKILLS_DIR = Path(__file__).resolve().parent

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_IF_RE = re.compile(
    r"\{%\s*if\s+(\w+)\s*%\}(.*?)(?:\{%\s*else\s*%\}(.*?))?\{%\s*endif\s*%\}",
    re.DOTALL,
)


#: 技能的两个「受众」。crew 是剧组的三个角色（提示词由代码按名加载），
#: main 是主对话 Agent（通过 load_skill 工具按需加载）。
#: 分受众的原因：主 Agent 的技能目录会进系统提示词，把剧组四个技能也列进去
#: 等于每轮都白付一份用不上的目录，还会诱导主 Agent 去加载「导演分镜方法论」
#: 这种它根本不该执行的东西。
AUDIENCES = ("crew", "main")
DEFAULT_AUDIENCE = "crew"


@dataclass
class SkillMeta:
    """技能的元数据（只读 frontmatter，不读正文）。"""

    name: str
    description: str
    when_to_use: str = ""
    path: Path | None = None
    audience: str = DEFAULT_AUDIENCE

    def to_line(self) -> str:
        """渲染成目录里的一行（给模型看的短描述）。"""
        when = f"（{self.when_to_use}）" if self.when_to_use else ""
        return f"- {self.name}：{self.description}{when}"


@dataclass
class Skill:
    """一个完整加载的技能。"""

    meta: SkillMeta
    body: str
    files: list[str] = field(default_factory=list)


class SkillNotFound(KeyError):
    """技能不存在。"""


def _skill_dir(name: str) -> Path:
    # 只允许简单的目录名，防止路径穿越
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name or ""):
        raise SkillNotFound(f"非法的技能名：{name!r}")
    return SKILLS_DIR / name


def _parse(path: Path) -> Skill:
    raw = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError(f"{path} 缺少 frontmatter（应以 --- 开头，含 name/description）")
    meta_raw = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta_raw, dict):
        raise ValueError(f"{path} 的 frontmatter 不是 mapping")
    name = str(meta_raw.get("name") or path.parent.name).strip()
    desc = str(meta_raw.get("description") or "").strip()
    if not desc:
        raise ValueError(f"{path} 缺少 description（它决定模型会不会加载这个技能）")
    # 受众写错必须吵：写成 "mian" 会让技能既进不了主 Agent 的目录、又混进剧组目录，
    # 表现为「技能存在但永远不被加载」——这种静默失效最难查。
    audience = str(meta_raw.get("audience") or DEFAULT_AUDIENCE).strip().lower()
    if audience not in AUDIENCES:
        raise ValueError(
            f"{path} 的 audience={audience!r} 非法（只能是 {' / '.join(AUDIENCES)}）"
        )
    body = m.group(2).strip()
    siblings = sorted(
        p.name for p in path.parent.iterdir()
        if p.is_file() and p.name != "SKILL.md" and not p.name.startswith(".")
    )
    return Skill(
        meta=SkillMeta(
            name=name,
            description=desc,
            when_to_use=str(meta_raw.get("when_to_use") or "").strip(),
            path=path,
            audience=audience,
        ),
        body=body,
        files=siblings,
    )


@lru_cache(maxsize=1)
def _all_skills() -> dict[str, Skill]:
    """扫描并缓存全部技能（磁盘上的文件在进程生命周期内不变）。"""
    found: dict[str, Skill] = {}
    if not SKILLS_DIR.exists():
        return found
    for child in sorted(SKILLS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            skill = _parse(skill_md)
        except Exception as e:  # noqa: BLE001
            # 单个技能坏掉不该让整个应用起不来；但要吵得让人听见。
            log.error("技能 %s 解析失败：%s", child.name, e)
            continue
        found[skill.meta.name] = skill
    return found


def parse_skill_text(raw: str, *, name_hint: str = "") -> Skill:
    """解析一段技能文本（frontmatter + 正文）。

    文件来源与「用户导入」共用这一份解析规则——两套规则迟早会跑偏。
    """
    m = _FRONTMATTER_RE.match(raw.strip())
    if not m:
        raise ValueError("缺少 frontmatter（应以 --- 开头，含 name / description）")
    meta_raw = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta_raw, dict):
        raise ValueError("frontmatter 不是 mapping")
    name = str(meta_raw.get("name") or name_hint or "").strip()
    desc = str(meta_raw.get("description") or "").strip()
    if not name:
        raise ValueError("缺少 name")
    if not desc:
        raise ValueError("缺少 description（它决定模型会不会加载这个技能）")
    audience = str(meta_raw.get("audience") or DEFAULT_AUDIENCE).strip().lower()
    if audience not in AUDIENCES:
        raise ValueError(f"audience={audience!r} 非法（只能是 {' / '.join(AUDIENCES)}）")
    return Skill(
        meta=SkillMeta(
            name=name,
            description=desc,
            when_to_use=str(meta_raw.get("when_to_use") or "").strip(),
            path=None,
            audience=audience,
        ),
        body=m.group(2).strip(),
        files=[],
    )


def _db_skills() -> dict[str, Skill]:
    """用户导入的技能（存数据库）。数据库不可用时返回空，不影响内置技能。

    复用 deps 的 Store 单例而不是每次新建：此前每次调用都
    ``Store(get_settings().database_url)`` 新建引擎并跑 create_all +
    _ensure_columns 迁移检查——system_prompt()/load_skill 每个对话轮都要读目录，
    等于每轮白建一遍数据库引擎（2026-11-11 审计）。
    """
    try:
        from ..deps import store as _store

        rows = _store().list_user_skills()
    except Exception as e:  # noqa: BLE001
        log.warning("读取用户技能失败（忽略）: %s", e)
        return {}
    out: dict[str, Skill] = {}
    for row in rows:
        try:
            out[row["name"]] = parse_skill_text(row["content"], name_hint=row["name"])
        except Exception as e:  # noqa: BLE001
            log.error("用户技能 %s 解析失败：%s", row.get("name"), e)
    return out


def _all_skills_with_db() -> dict[str, Skill]:
    """文件技能 + 用户导入技能（同名时用户导入的优先？——不，文件优先）。

    文件里的内置技能是「官方维护」的，名字被用户抢走会让官方技能失效，
    所以同名时**内置胜出**，用户导入的改名提示。
    """
    merged = dict(_db_skills())
    merged.update(_all_skills())
    return merged


def list_skills(audience: str | None = None) -> list[SkillMeta]:
    """列出技能的**元数据**（渐进披露第一级，很便宜）。

    传 audience 只列该受众的技能；不传列全部（向后兼容）。
    """
    metas = [s.meta for s in _all_skills_with_db().values()]
    if audience is not None:
        metas = [m for m in metas if m.audience == audience]
    return metas


def load_skill(name: str) -> Skill:
    """加载一个技能的完整内容（第二级）。"""
    allskills = _all_skills_with_db()
    skill = allskills.get(name)
    if skill is None:
        raise SkillNotFound(f"未知技能：{name}（可用：{', '.join(allskills) or '无'}）")
    return skill


def skill_file(name: str, filename: str) -> str:
    """读取技能的附属文件（第三级，按需）。"""
    skill = load_skill(name)
    if filename not in skill.files:
        raise SkillNotFound(f"技能 {name} 没有附属文件 {filename}")
    base = _skill_dir(name)
    target = (base / filename).resolve()
    if base.resolve() not in target.parents:
        raise SkillNotFound(f"非法的附属文件路径：{filename}")
    return target.read_text(encoding="utf-8")


def skill_index(audience: str | None = None) -> str:
    """技能目录（给模型看的短清单，不含正文）。"""
    metas = list_skills(audience)
    if not metas:
        return "（没有可用技能）"
    return "\n".join(m.to_line() for m in metas)


def render(text: str, variables: dict[str, object] | None = None) -> str:
    """渲染模板：先展开条件块，再替换变量。

    条件块只做一层（不支持嵌套）——我们的用法就是「有锚 / 无锚」这种二选一，
    支持嵌套会让模板调试变复杂，收益为零。
    """
    variables = variables or {}

    def _truthy(value: object) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        return bool(value)

    def _if(m: re.Match[str]) -> str:
        name, then_part, else_part = m.group(1), m.group(2), m.group(3)
        if name not in variables:
            raise KeyError(f"模板引用了未提供的条件变量：{name}")
        return then_part if _truthy(variables[name]) else (else_part or "")

    # 条件块可能嵌套在字符串里，循环到稳定为止（实测一次就够，留个安全上限）
    for _ in range(5):
        new = _IF_RE.sub(_if, text)
        if new == text:
            break
        text = new

    missing: list[str] = []

    def _var(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in variables:
            missing.append(name)
            return m.group(0)
        return str(variables[name])

    text = _VAR_RE.sub(_var, text)
    if missing:
        raise KeyError(f"模板引用了未提供的变量：{', '.join(sorted(set(missing)))}")
    return text.strip()


def render_skill(name: str, **variables: object) -> str:
    """加载并渲染一个技能。"""
    return render(load_skill(name).body, variables)


def clear_cache() -> None:
    """清缓存（测试用：改完 SKILL.md 不必重启）。"""
    _all_skills.cache_clear()
