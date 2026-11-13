# =====================================================================
# approvals.py —— 提示词批准的服务端硬闸门（D-43）
#
# 问题：出片要花钱，「批准」原本只是提示词级约束——approve 是个普通工具，
# 模型可以在同一轮里自己批准、自己出片；小说原文还会进上下文（注入面）。
# 「不替用户做最终决定」写在系统提示里，但没有任何服务端机制兜底。
#
# 方案：把「批准」拆成两步。
#   1. 模型调 approve_video_prompts → 只登记 PendingApproval（见 store.py），
#      不写任何 approved 状态；
#   2. 真正落状态只走本模块 apply_approval，调用方全部是**确定性路径**：
#      a. 用户点对话里的确认卡片（meta.intent=approve_prompts，浏览器发的）；
#      b. 用户下一条消息被 detect_confirmation 判为确认（消息来自 API 请求体，
#         模型伪造不了——它能生成的只有上一轮的 assistant 文本）。
#   UI 工作台的逐镜批准按钮本来就在浏览器侧直接调 REST，不经过这里。
#
# detect_confirmation 走「保守」原则：宁可漏判（用户再点一次卡片）也不误判
# （模型诱导一句带「批准」的话就放行付费出片）。疑问句、否定句、条件句、
# 长消息一律不算确认。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

from ..store import Store

log = logging.getLogger(__name__)

# 确认意图的关键词：短消息里出现这些才继续判断。
_CONFIRM_WORDS = ("批准", "同意", "确认批准", "批准全部", "全部批准", "都批准")
# 否定/条件词：出现任何一条直接否决（保守：只认干脆的肯定答复）。
_DENY_MARKS = (
    "不批准", "别批准", "先不", "暂不", "还没", "未批准", "不要", "取消",
    "等", "再看", "等等", "如果", "万一", "需不需要", "要不要",
)
# 疑问标记：问句不是确认。
_QUESTION_MARKS = ("吗", "？", "?", "哪些", "几个", "怎么", "如何", "看看", "查", "列出")


def detect_confirmation(message: str) -> bool:
    """用户消息是否可以视为「确认批准」？（确定性，宁漏勿误）

    只在存在待确认请求时调用（conductor 每轮先查 pending）。判定条件：
    - 短消息（≤30 字）——长篇大论大概率是在讨论而不是拍板；
    - 含确认词；
    - 不含否定/条件词、不含疑问标记。
    """
    text = (message or "").strip()
    if not text or len(text) > 30:
        return False
    if not any(w in text for w in _CONFIRM_WORDS):
        return False
    if any(w in text for w in _DENY_MARKS):
        return False
    if any(w in text for w in _QUESTION_MARKS):
        return False
    return True


def apply_approval(store: Store, project_id: str, version_id: str, shot_ids: list[str]) -> dict[str, Any]:
    """真正执行批准：把目标镜头 prompt_status 置 approved，生成新镜头版本。

    只允许确定性路径调用（卡片动作 / 文本确认），返回
    {"ok": bool, "message": str, "approved_count": int, "version_id": str}。
    校验全部 fail-closed：版本归属、镜头存在、有提示词、未批准过。
    """
    from ..crew.dramaturgy import refresh_prompt_audits

    version = store.get_video_version(version_id)
    if version is None or version.project_id != project_id:
        return {"ok": False, "message": "镜头方案不存在或不属于该作品，批准未生效。", "approved_count": 0}

    shots = [dict(s) for s in (version.shots or []) if isinstance(s, dict)]
    wanted = {s.strip() for s in shot_ids if str(s).strip()}

    def _candidate(s: dict[str, Any]) -> bool:
        return bool(s.get("id")) and bool(s.get("video_prompt")) and s.get("prompt_status") != "approved"

    if wanted:
        targets = [s for s in shots if s.get("id") in wanted and _candidate(s)]
        missing = [sid for sid in wanted
                   if sid not in {s.get("id") for s in shots if s.get("video_prompt")}]
        if missing and not targets:
            return {"ok": False,
                    "message": f"镜头 {'、'.join(missing)} 不在当前方案里或没有提示词，批准未生效。",
                    "approved_count": 0}
    else:
        targets = [s for s in shots if _candidate(s)]

    if not targets:
        return {"ok": False, "message": "没有可批准的镜头（可能都已批准，或选中镜头没有提示词）。", "approved_count": 0}

    for s in targets:
        s["prompt_status"] = "approved"
        s["prompt_review_note"] = "对话中批准（用户确认）"
    # style_guide 是 dict property——原工具里的 `.camera_style` 属性访问在
    # 非空时其实会 AttributeError（隐藏炸点），这里顺手改成安全的 get 取法。
    style_context = str((version.style_guide or {}).get("camera_style") or "")
    refresh_prompt_audits(shots, style_context=style_context)
    new_version = store.create_video_version(
        project_id=project_id, shots=shots,
        style_guide=version.style_guide,
        source_type="manual", label="用户确认批准镜头提示词",
        notes=None, parent_version_id=version.id,
    )
    names = "、".join(s["id"] for s in targets)
    return {
        "ok": True,
        "message": f"已批准 {len(targets)} 个镜头的提示词（{names}），新镜头版本 {new_version.id}。批准卡片上已长出「出片」按钮，用户点一下就能提交视频生成——不用他打字。",
        "approved_count": len(targets),
        "version_id": new_version.id,
    }
