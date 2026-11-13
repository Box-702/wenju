# =====================================================================
# seed.py —— .env → DB 的一次性迁移（D-48：env 彻底退出模型配置）
#
# 历史：D-41 时代 .env 是空库兜底。2026-11-13 用户定案「模型设置完全移除
# env，只走 BYOK」——解析层（registry.py）已删掉全部 env 链，本模块只剩
# 一个职责：**把老安装 .env 里已有的 key 搬进 DB，一次，只补缺**。
# 不直接删掉本模块的原因：那会让所有靠 .env 跑着的老安装一夜之间变
# 「未配置」。迁移完成后 DB 就是唯一权威，界面引导用户贴 key。
#
# env 仅存的两个豁免（都不是配置入口）：
#   - MINIMAX_API_KEY_FALLBACKS：备用 key 故障转移安全网（app/keys.py）；
#   - DEEPSEEK_THINKING：行为开关（思考链），不选模型不选 key。
#
# 原则不变：**只补缺，绝不覆盖，绝不替用户改选择。**幂等，跑一百遍结果一致。
# =====================================================================

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def seed_keys_from_env(settings: Any, store: Any) -> list[str]:
    """把 .env 里现有 key 幂等搬进 api_providers（只补缺）。返回本次新建的行清单。"""
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

    def env_first(*names: str) -> str:
        # 直接读 os.environ（bootstrap_env 已把 .env 落进来）——resolver
        # 已 DB-only，迁移就是要绕开它看一眼老配置。
        for n in names:
            v = (os.environ.get(n, "") or "").strip()
            if v:
                return v
        return ""

    # ---- 对话：env 链原优先级（OPENAI > ZHIPUAI > DEEPSEEK）----
    if k := env_first("OPENAI_API_KEY"):
        ensure_row("llm", "openai", "OpenAI 兼容", k,
                   env_first("OPENAI_BASE_URL"), env_first("OPENAI_MODEL"))
    if k := env_first("ZHIPUAI_API_KEY"):
        ensure_row("llm", "zhipu", "智谱 GLM", k,
                   env_first("ZHIPUAI_BASE_URL"), env_first("ZHIPUAI_MODEL_NAME"))
    if k := env_first("DEEPSEEK_API_KEY"):
        ensure_row("llm", "deepseek", "DeepSeek", k,
                   env_first("DEEPSEEK_BASE_URL"), env_first("DEEPSEEK_MODEL_NAME"))
    if k := env_first("MINIMAX_API_KEY"):
        ensure_row("llm", "minimax", "MiniMax", k,
                   env_first("MINIMAX_BASE_URL"), env_first("MINIMAX_MODEL_NAME"))

    # ---- 文生图（cogview 与智谱共用 key） ----
    if k := env_first("IMAGE_API_KEY", "ZHIPUAI_API_KEY"):
        ensure_row("image", "cogview", "智谱", k, "",
                   env_first("IMAGE_MODEL"))

    # ---- 配音 ----
    if k := env_first("TTS_API_KEY", "MINIMAX_API_KEY"):
        ensure_row("tts", "minimax", "MiniMax 语音合成", k, "",
                   env_first("TTS_MODEL"))

    # ---- 视频：MiniMax 一把 key 打通全链 ----
    # 备用 key 轮换（MINIMAX_API_KEY_FALLBACKS，见 app/keys.py）继续走
    # os.environ——那是故障转移安全网，不是配置入口。
    if k := env_first("MINIMAX_API_KEY"):
        ensure_row("video", "minimax", "MiniMax", k, "", "")

    if actions:
        log.info("BYOK 一次性迁移（幂等，只补缺不覆盖）：%s", "、".join(actions))
    return actions
