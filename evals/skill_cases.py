# =====================================================================
# skill_cases.py —— 主 Agent 技能加载的评测用例
#
# 测的是「拆开成四个技能到底有没有用」，不是测提示词质量（那是 evals/cases.py 的事）。
#
# 三个可观测维度：
#   1. 触发对不对 —— 该领域加载了对应技能吗
#   2. 串味没有   —— 有没有加载不相关的技能（一个技能包含全部细则 = 退化成一个大提示词）
#   3. 规则落没落地 —— 技能里的关键约束，行为上兑现了吗
#
# 第 3 条是重点：它衡量的是「细则」的价值。系统提示里本来就有触发器表
# （意图 → 工具），所以光看「调对工具」证明不了技能有用；
# 有用的是那些**触发器表里没有、只写在技能正文里的细则**
# （如「出片前必须先确认提示词已批准」）。
# =====================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# 一次对话的工具调用序列
ToolCall = dict[str, Any]


@dataclass
class SkillCase:
    """一条技能加载用例。"""

    id: str
    name: str
    #: 发给主 Agent 的用户请求
    request: str
    #: 期望加载的技能（None = 期望一个都不加载）
    expect_skill: str | None
    #: 期望**不要**加载的技能（串味检查）
    forbid_skills: list[str] = field(default_factory=list)
    #: 期望出现的工具调用（顺序敏感时用 order_sensitive）
    expect_tools: list[str] = field(default_factory=list)
    #: 期望**不要**出现的工具调用
    forbid_tools: list[str] = field(default_factory=list)
    #: 期望按此相对顺序出现的工具（后面的必须在前面的之后）
    expect_order: list[str] = field(default_factory=list)
    #: 期望出现在最终回复里的关键词（任一命中即可）
    expect_reply_contains: list[str] = field(default_factory=list)
    #: 是否需要先建项目
    need_project: bool = True
    #: 要不要预置项目内容，让领域动作**可执行**：
    #:   "script" —— 先生成剧本版本（否则 Agent 只能查状态说「还没剧本」，
    #:                   测出来的是「它知道自己没剧本」而不是「技能引导了它」）
    #:   "shots"  —— 再往下跑到分镜 + 视频提示词，且全部标成**待批准**
    #:                   （只有真的有未批准的镜头，「出片前要先批准」才被考验）
    prepare: str = ""
    #: 需要多长的原文（长篇用例）
    raw_text: str = ""
    #: 备注：这个用例在验证什么
    why: str = ""


# ---------------------------------------------------------------------
# 工具序列的判定辅助
# ---------------------------------------------------------------------


def called_tools(events: list[dict[str, Any]]) -> list[str]:
    """从事件里取出工具调用序列（按发生顺序）。"""
    return [
        str(e.get("name") or "")
        for e in events
        if e.get("type") == "tool_call"
    ]


def loaded_skills(events: list[dict[str, Any]]) -> list[str]:
    """取**真正加载成功**的技能名（按顺序、去重）。

    ⚠️ 只数成功的：消融对照组里 `load_skill` 工具被摘掉了，但系统提示仍然写着
    「本轮第一个工具调用就应该是 load_skill」——模型照样会**尝试**调用这个不存在的
    工具。循环把它记成 tool_call 事件、返回「未知工具」。
    如果把「尝试」当成「加载成功」，消融组就会显示技能被加载了——测量直接失效。
    所以这里用工具结果排除掉失败的那些。
    """
    # 先把每个 load_skill 调用的结果对上（按出现顺序）
    calls: list[str] = []
    results: list[str] = []
    for e in events:
        etype = e.get("type")
        if etype == "tool_call" and e.get("name") == "load_skill":
            calls.append(_skill_arg(e))
            results.append("")      # 占位，与调用一一对应
        elif etype == "tool_result" and e.get("name") == "load_skill":
            if results:
                results[-1] = str(e.get("summary") or "")

    out: list[str] = []
    for name, result in zip(calls, results, strict=True):
        if not name:
            continue
        # 失败的样子有三种：
        #   「没有这个技能：…」  —— 工具在但名字不对（技能加载器自己返回的）
        #   「未知工具：…」      —— 工具整个不存在（消融组：load_skill 被摘掉了）
        #   「技能 X 读取失败」  —— 文件坏了
        if result.startswith("没有这个技能") or result.startswith("未知工具"):
            continue
        if result.startswith("技能 ") and "读取失败" in result:
            continue
        if name not in out:
            out.append(name)
    return out


def attempted_skills(events: list[dict[str, Any]]) -> list[str]:
    """被**尝试**加载的技能名（不管成功与否）——用来区分「没加载」与「加载失败」。"""
    out: list[str] = []
    for e in events:
        if e.get("type") == "tool_call" and e.get("name") == "load_skill":
            name = _skill_arg(e)
            if name and name not in out:
                out.append(name)
    return out


def _skill_arg(event: dict[str, Any]) -> str:
    """从 tool_call 事件里取出 load_skill 的 name 参数。"""
    args = event.get("args")
    if isinstance(args, dict):
        return str(args.get("name") or "")
    if isinstance(args, str):
        try:
            import json

            return str(json.loads(args).get("name") or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def index_of(tools: list[str], name: str) -> int:
    """某个工具第一次出现的位置；没有则 -1。"""
    return tools.index(name) if name in tools else -1


# ---------------------------------------------------------------------
# 用例集
# ---------------------------------------------------------------------

_LONG_TEXT = (
    "第一章　雨夜\n"
    "凌晨三点，滨江路的路灯在雨里像一团化不开的黄。林然把车停在旧楼前，熄了火，"
    "雨水顺着挡风玻璃往下淌。收音机里播着今天的新闻：城东旧货市场失火，无人伤亡。"
    "他盯着那栋楼，深吸一口气，推开车门。楼里没有灯，走廊尽头的铁门虚掩着，"
    "锁扣上挂着一根断了一半的铜钥匙。林然捡起来看了看，铜钥上面刻着一行小字：十四。"
    "他走进铁门，脚下是潮湿的水泥地，空气里有股烧焦的霉味。\n"
) * 90  # ≈ 1.5 万字，超过 1.4 万字的阈值


ALL_SKILL_CASES: list[SkillCase] = [
    SkillCase(
        id="delivery_before_approve",
        name="出片前先确认提示词已批准",
        request="出片吧，把这个项目的镜头都生成视频。",
        expect_skill="production-delivery",
        forbid_skills=["novel-serial", "script-review"],
        # 技能正文的硬门槛：提示词未批准不能直接提交
        forbid_tools=["submit_videos"],
        expect_tools=["get_status"],
        expect_reply_contains=["批准", "提示词"],
        # 关键：项目里必须真的有**待批准**的镜头，否则这条约束无从谈起
        #（空项目上 Agent 自然会查状态，测出来的是「它本来就会查」而不是「技能让它查」）
        prepare="shots",
        why="production-delivery 的核心价值是「三个前置」。系统提示只说「出片→submit_videos」，"
            "「没批准就别提交」这条细则只写在技能里——所以这条用例能证明细则有用。",
    ),
    SkillCase(
        id="crew_order",
        name="拆镜头与写提示词的顺序",
        request="给这个剧本拆镜头，然后写好每个镜头的视频提示词。",
        expect_skill="crew-pipeline",
        forbid_skills=["novel-serial", "production-delivery"],
        expect_tools=["breakdown_scenes", "generate_style_guide"],
        # 依赖顺序：拆解 → 风格指南 → 提示词。提示词依赖风格指南的锚，
        # 所以「拆解完先出风格指南」才是对的；不必强求它一路走到提示词。
        expect_order=["breakdown_scenes", "generate_style_guide"],
        # 需要有剧本，拆解才跑得起来；否则 Agent 只能回「还没有剧本」
        prepare="script",
        why="crew-pipeline 讲的是「依赖关系是硬的，不是建议」：提示词依赖拆解结果。"
            "如果乱序，先写提示词就是对着空气写。",
    ),
    SkillCase(
        id="revision_not_direct_edit",
        name="改编走提议而不是直接改稿",
        request="把第二场那句对白改口语化一点。",
        expect_skill="script-review",
        forbid_skills=["novel-serial", "crew-pipeline"],
        expect_tools=["run_adaptation"],
        # 有剧本才谈得上改编；否则 Agent 回「还没有剧本」就停了
        prepare="script",
        why="script-review 的铁律是「改编不会直接改掉剧本」：要出提议、给用户逐条决定。"
            "模型自己改稿就绕过了人审。",
    ),
    SkillCase(
        id="long_novel_truncation",
        name="长篇不直接生成初稿",
        request="把这部小说改编成短剧剧本。",
        expect_skill="novel-serial",
        forbid_skills=["crew-pipeline", "production-delivery"],
        expect_tools=["map_novel"],
        forbid_tools=["generate_script"],
        raw_text=_LONG_TEXT,
        why="novel-serial 防的是**静默截断**：一次只吃得下 1.2 万字，20 万字直接生成初稿，"
            "模型只写开头一小段，剩下的被静默丢弃，而界面显示「生成成功」。"
            "这条规则只在技能正文里，系统提示只有一句「长篇要先建图」。",
    ),
    SkillCase(
        id="no_over_trigger",
        name="无关请求不加载任何技能",
        request="帮我把这段会议记录整理成一份待办清单。",
        expect_skill=None,
        forbid_skills=["novel-serial", "crew-pipeline", "script-review", "production-delivery"],
        need_project=False,
        why="反向用例：技能机制的失败模式之一是**过度触发**——什么都 load_skill，"
            "目录失去筛选作用，token 白花。四个技能拆开的另一个理由就是让「不该加载」"
            "变得容易判断。",
    ),
]
