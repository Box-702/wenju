# =====================================================================
# profiles.py —— 改编类型（Adaptation Profile）配置
#
# 产品只做一件事：**小说 → 短剧**。所以这里只有短剧一份 profile，
# 外加一个 `other` 兜底（读老项目时用）。
#
# 曾经有 film / series / stage 三份，各写一套「场景规划 / 剧本节拍」指导。
# 它们的问题不是写得不好，而是**产品根本不会走到**：多一个类型就多一条
# 生成分支、多一套测试、多一处没人维护的文案。收敛到一个类型后，
# 提示词可以只为「竖屏、1–2 分钟、前 3 秒抓人」这一件事调到最好。
#
# 注意：domain.AdaptationType 仍保留 film/series/stage 枚举值——那是为了
# 读得动历史数据，不是为了继续提供这些类型。新产品入口只暴露短剧。
# =====================================================================

from __future__ import annotations

import json
from typing import Any, Literal

# 支持的改编类型，与 domain.AdaptationType 对齐。
ProfileName = Literal["short_drama", "other"]


# ---------- 类型定义 ----------

PROFILES: dict[str, dict[str, Any]] = {
    "short_drama": {
        "label": "短剧",
        "target_format": "竖屏或横屏短剧，单场节奏紧凑",
        "tone": "high-tension",
        "scene_planning": [
            "优先保留强钩子、快速反转和高冲突场面。",
            "每场只服务一个清晰戏剧目标，减少解释性过场。",
            "场景结尾尽量形成追问、危险或情绪悬念。",
        ],
        "script_blocks": [
            "动作短、可拍、直接推动下一句对白。",
            "对白口语化，避免长段说明。",
            "前三个节拍尽快暴露压力、秘密或危险。",
            # 片长是从剧本层开始约束的：下游再努力也压不回来。
            # 实测《孔乙己》剧本写了 471 字对白（≈118 秒纯念白），
            # 下游只能拆出 62 个镜头、5 分钟成片——不是短剧。
            "**对白总量控制**：单集 55–95 秒，全片对白 150–220 字（约占一半时长），"
            "其余留给动作与停顿。原文大段对话要合并、删减或改成动作，不要逐句照搬。",
            "场景数按素材量定：几百字的微型素材 3–5 场，常规一集 5–7 场；"
            "每场只服务一个戏剧目标，不为过场单独开场。",
        ],
    },
    "other": {
        "label": "自定义改编",
        "target_format": "自定义剧本格式",
        "tone": "adaptive",
        "scene_planning": [
            "优先保留原作核心冲突、人物关系和可改编场面。",
            "按素材自然选择场景密度，不强行套模板。",
        ],
        "script_blocks": [
            "动作与对白按时间顺序组织，方便人工继续改写。",
            "保留必要潜台词和改编说明。",
        ],
    },
}

# 默认类型名。
DEFAULT_PROFILE: ProfileName = "short_drama"


def profile_for(name: str) -> dict[str, Any]:
    """按名称取 profile；未知名称回退到 ``other``。"""
    key = normalize_name(name)
    return PROFILES.get(key, PROFILES["other"])


def normalize_name(name: str) -> str:
    """把外部传来的类型名规整到 ProfileName。"""
    from ..domain import normalize_adaptation_type

    return normalize_adaptation_type(name)


def profile_type(name: str) -> ProfileName:
    """返回受约束的类型名（用于持久化 / 枚举）。"""
    return normalize_name(name)  # type: ignore[return-value]


def profile_prompt(profile: dict[str, Any], *, language: str) -> str:
    """把某个 profile 序列化成可读的 JSON 提示词片段。"""
    return json.dumps(
        {
            "adaptation_label": profile["label"],
            "target_format": profile["target_format"],
            "tone": profile["tone"],
            "scene_planning": profile["scene_planning"],
            "script_blocks": profile["script_blocks"],
            "output_language": language,
        },
        ensure_ascii=False,
        indent=2,
    )
