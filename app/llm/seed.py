# =====================================================================
# seed.py —— 把 .env 现有配置幂等入库，让 DB 成为唯一权威（D-41）
#
# 为什么需要：原来读取优先级是「.env 压过 DB」——用户在设置里选的模型会被
# .env 悄悄盖掉（实测：UI 选了 deepseek-v4-pro，实际跑的却是 env 的
# deepseek-flash）。BYOK 产品里 key 和选择都是用户的，界面必须说话算数。
#
# 原则：**只补缺，绝不覆盖，也绝不替用户改选择。**
#   - 某 (kind, name) 已有带 key 的行 → 跳过（那是用户在 UI 里定的，原样生效）；
#   - DB 里还没有的模态 → 按 env 当前实际生效的配置建一行（保住"能跑"）；
#   - 不设 model_preference：单行时 DB 兜底自然选中它，多行时由用户在 UI 里选，
#     种子不越俎代庖（设偏好反而可能把用户的选择盖回 env 的字面值）。
#   - env 里没有对应 key 就什么都不做——空库保持空库（新装靠 env 兜底起跑）。
#
# 幂等：跑一百遍结果一致，用户数据不会被改坏。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def seed_keys_from_env(settings: Any, store: Any) -> list[str]:
    """把 .env 里现有 key 与当前生效配置幂等写进 api_providers（只补缺）。

    返回本次新建的行清单（供日志），空列表 = 什么都没做。
    """
    actions: list[str] = []

    def find_row(kind: str, name: str):
        return next(
            (r for r in store.list_api_providers(kind=kind)
             if (r.name or "") == name and (r.api_key or "")),
            None,
        )

    def ensure_row(kind: str, name: str, label: str, api_key: str,
                   base_url: str, model: str):
        # 已有带 key 的同名行：那是用户的选择，原样保留，绝不覆盖。
        if find_row(kind, name) is not None:
            return
        store.create_api_provider(
            kind=kind, name=name, label=label or name,
            base_url=base_url or "", api_key=api_key,
            config={"model": model} if model else {},
        )
        actions.append(f"{kind}/{name}")

    # store=None 的 resolver = 「env 视图」：现在真正在跑什么，就搬什么。
    from .registry import resolve_chat, resolve_image, resolve_tts

    # ---- 对话：env 链当前生效的那家 + 实际模型名 ----
    cur = resolve_chat(settings, None)
    if cur is not None and cur.api_key:
        ensure_row("llm", cur.provider, cur.label, cur.api_key, cur.base_url, cur.model)

    # ---- 文生图（cogview 与智谱共用 key） ----
    img = resolve_image(settings, None)
    if img is not None and img.api_key:
        ensure_row("image", img.provider, img.label, img.api_key, img.base_url, img.model)

    # ---- 配音 ----
    tts = resolve_tts(settings, None)
    if tts is not None and tts.api_key:
        ensure_row("tts", tts.provider, tts.label, tts.api_key, tts.base_url, tts.model)

    # ---- 视频：MiniMax 一把 key 打通全链；env 主 key 入库 ----
    # 备用 key 轮换机制（app/keys.py）继续读 os.environ——那是故障转移的
    # 安全网，不是配置入口；主 key 以 DB 为准，UI 里改了就立刻生效。
    from ..keys import minimax_keys

    mm_key = (getattr(settings, "minimax_api_key", "") or "").strip()
    if not mm_key:
        ring = minimax_keys()
        mm_key = ring[0] if ring else ""
    if mm_key:
        ensure_row("video", "minimax", "MiniMax 视频", mm_key, "", "")

    if actions:
        log.info("BYOK 入库（幂等，只补缺不覆盖）：%s", "、".join(actions))
    return actions
