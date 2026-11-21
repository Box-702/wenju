# =====================================================================
# confirm.py —— 流程分叉确认卡（D-76，对标即梦「每一步都选项确认」）
#
# 即梦的 Agent 永不抢跑：改编重点、集数时长、音色方案、生成范围、模型画幅
# 这类分叉点，全部变成对话里的「确认方向」选项卡（radio + 描述 + 推荐标记），
# 用户拍板才推进。我们此前这类信息要么模型自己拍板（越权），要么散落在
# 设置弹窗里（用户不知道能选）。
#
# 机制与 D-43 批准闸门同型：ask_user_choice 只**发起** PendingChoice 并
# 结束本轮；真正的选择只走确定性路径（meta.intent=confirm_options，浏览器
# 发的），模型伪造不了。选择结果落项目记忆，下一轮 Agent 自然带上。
#
# questions 走 JSON 字符串而不是结构化参数：deepseek 对嵌套数组的格式遵循
# 不稳（见 DEVELOPMENT 深seek 格式实测），字符串 + 服务端容错解析 + 编造
# 护栏（坏结构直接打回让它重发）最可靠。
# =====================================================================

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from .context import ToolContext

_TITLE_MAX = 40
_Q_MAX = 80
_LABEL_MAX = 24
_DESC_MAX = 60


def _clean_questions(raw: Any) -> tuple[list[dict], list[str]]:
    """容错解析 questions：返回 (干净的问题列表, 丢弃原因列表)。

    护栏规则：问题最多 4 个；每题选项 2-6 个；超长截断；缺 label 的选项丢弃；
    整题没有合法选项就丢题。id 一律按位置重编（q{n}o{m}），确认回传时按
    位置对齐，模型给不给 id 都不影响。
    """
    cleaned: list[dict] = []
    dropped: list[str] = []
    if not isinstance(raw, list):
        return cleaned, ["questions 不是数组"]
    for qi, q in enumerate(raw[:4], start=1):
        if not isinstance(q, dict):
            dropped.append(f"问题{qi}：不是对象")
            continue
        text = str(q.get("question") or q.get("q") or "").strip()[:_Q_MAX]
        opts_raw = q.get("options") or []
        if not text or not isinstance(opts_raw, list):
            dropped.append(f"问题{qi}：缺问题文本或选项")
            continue
        options: list[dict] = []
        for oi, o in enumerate(opts_raw[:6], start=1):
            if not isinstance(o, dict):
                continue
            label = str(o.get("label") or o.get("name") or o.get("value") or "").strip()
            if not label:
                continue
            options.append({
                "id": f"q{qi}o{oi}",
                "label": label[:_LABEL_MAX],
                "desc": str(o.get("desc") or o.get("description") or "").strip()[:_DESC_MAX],
                "recommended": bool(o.get("recommended")),
            })
        if len(options) < 2:
            dropped.append(f"问题{qi}：合法选项不足 2 个")
            continue
        cleaned.append({
            "question": text,
            "multi": bool(q.get("multi")),
            "options": options,
        })
    return cleaned, dropped


def build(ctx: ToolContext) -> list:
    """装配确认卡工具。"""
    store = ctx.store

    @tool
    def ask_user_choice(project_id: str, title: str, questions_json: str) -> str:
        """把流程分叉整理成「确认方向」选项卡发给用户拍板——改编重点、集数与时长、音色方案、生成范围（全部/第1集/前半）、模型与画幅等一切「下一步怎么走」的选择，都必须用这个工具发卡确认，用户拍板前不要继续推进。参数：project_id=项目 id；title=卡片的阶段名（如「改编方向」「出片范围」，≤40 字）；questions_json=问题数组 JSON 字符串，每题 {"question":"问题文本","multi":false,"options":[{"label":"选项名","desc":"一句利弊说明","recommended":true}]}，recommended 可标一个推荐项。

        使用规矩：
        - 一次卡片问 1-3 个问题，每题 2-5 个选项；每个选项给一句 desc 说明取舍；
        - 用户确认前本轮必须结束（不再调其它工具、不再往下推进）——
          确认结果会作为消息出现在下一轮，你按结果继续。

        questions_json 示例：
        [{"question":"改编重点放在哪条线？","multi":false,
          "options":[{"label":"替身悬疑线","desc":"冲突集中，短剧节奏最强","recommended":true},
                     {"label":"家庭情感线","desc":"共情强但推进偏慢"}]}]
        """
        # 先校验后落库：坏输入直接打回重发，不去碰项目（坏 JSON 也不该炸 ValueError）
        try:
            raw = json.loads(questions_json or "[]")
        except json.JSONDecodeError:
            raw = None
        questions, dropped = _clean_questions(raw)
        if not questions:
            return (
                "确认卡没有可用的题（questions_json 解析失败或选项不足）。"
                "请按工具说明重发：数组里每题要有 question 和至少 2 个带 label 的 options。"
                + (f"被丢弃：{'；'.join(dropped)}。" if dropped else "")
            )
        p = ctx.project(project_id)
        conv = ctx.collector.get("conversation_id") or ""
        request = store.create_pending_choice(
            project_id=p.id, conversation_id=conv,
            title=str(title or "").strip()[:_TITLE_MAX] or "确认方向",
            questions=questions,
        )
        ctx.collector["payloads"].append({
            "type": "option_confirm",
            "choice_id": request.id,
            "project_id": p.id,
            "title": request.title,
            "questions": questions,
            "status": "pending",
        })
        qn = "、".join(q["question"] for q in questions)
        return (
            f"已发出确认卡「{request.title}」（{qn}）。"
            "**还没有任何决定生效**——本轮到此为止，等用户在卡片上点选并确认；"
            "下一轮他的选择会作为消息进来，按选择继续，不要替用户预设答案。"
        )

    return [ask_user_choice]
