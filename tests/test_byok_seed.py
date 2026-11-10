# =====================================================================
# test_byok_seed.py —— 真 BYOK：DB 权威 + .env 幂等入库（D-41）
#
# 锁住两件事：
#   1. 优先级：DB 有配置时压过 env（用户在 UI 选的必须说话算数）；
#      DB 空时 env 兜底（新装/删库仍能靠 .env 起跑）。
#   2. 种子：只补缺不覆盖、不设偏好、幂等——跑一百遍不改坏用户数据。
# =====================================================================

from __future__ import annotations

from app.config import Settings
from app.llm.registry import resolve_chat
from app.llm.seed import seed_keys_from_env


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "sqlite:///./data/_unused.db",
        "CHECKPOINTER": "memory",
        "OPENAI_API_KEY": "",
        "ZHIPUAI_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "MINIMAX_API_KEY": "",
        "MINIMAX_API_KEY_FALLBACKS": "",
        "CHECKPOINT_DSN": "",
        "CHAT_PROVIDER": "",
        "VISION_PROVIDER": "",
        "VISION_API_KEY": "",
        "IMAGE_PROVIDER": "",
        "IMAGE_API_KEY": "",
    }
    base.update(overrides)
    return Settings(**base)


# ---------- 优先级：DB 压过 env ----------


def test_db_row_beats_env_chain(store):
    """DB 里有对话厂商时，即使 env 也有 key，也必须用 DB 的（UI 选择优先）。"""
    store.create_api_provider(
        kind="llm", name="deepseek", label="DeepSeek",
        base_url="https://api.deepseek.com", api_key="db-key",
        config={"model": "deepseek-v4-pro"},
    )
    # env 指向另一家（openai），若优先级没翻转就会选到 openai
    cfg = resolve_chat(_settings(OPENAI_API_KEY="env-oa", CHAT_PROVIDER="openai"), store)
    assert cfg.provider == "deepseek"
    assert cfg.model == "deepseek-v4-pro"
    assert cfg.source == "db"


def test_empty_store_falls_back_to_env(store):
    """DB 空 → env 兜底（保持'克隆下来就能跑'，也保持无 store 的既有行为）。"""
    cfg = resolve_chat(_settings(DEEPSEEK_API_KEY="env-ds", CHAT_PROVIDER="deepseek"), store)
    assert cfg is not None
    assert cfg.provider == "deepseek"
    assert cfg.source == "env"


def test_no_store_no_env_returns_none(store):
    assert resolve_chat(_settings(), store=None) is None


# ---------- 种子：只补缺、不覆盖、幂等 ----------


def test_seed_is_idempotent(store):
    """跑两遍：第二遍什么都不做（不产生重复行）。"""
    s = _settings(DEEPSEEK_API_KEY="ds", MINIMAX_API_KEY="mm", ZHIPUAI_API_KEY="zp")
    first = seed_keys_from_env(s, store)
    assert first, "首次应补齐 env 里的配置"
    second = seed_keys_from_env(s, store)
    assert second == [], "第二次必须空跑（幂等）"
    # 每个 (kind,name) 只有一行
    rows = [(r.kind, r.name) for r in store.list_api_providers()]
    assert len(rows) == len(set(rows)), f"出现重复行：{rows}"


def test_seed_never_overwrites_existing_key(store):
    """已有带 key 的行是用户在 UI 定的，种子绝不覆盖。"""
    store.create_api_provider(
        kind="llm", name="deepseek", label="DeepSeek",
        base_url="https://api.deepseek.com", api_key="user-key",
        config={"model": "deepseek-v4-pro"},
    )
    seed_keys_from_env(_settings(DEEPSEEK_API_KEY="env-key"), store)
    row = next(r for r in store.list_api_providers(kind="llm") if r.name == "deepseek")
    assert row.api_key == "user-key", "种子把用户 key 覆盖成 env 的了"


def test_seed_does_not_set_preference(store):
    """种子不越俎代庖设默认偏好——多行时由用户在 UI 选，设偏好会盖掉选择。"""
    seed_keys_from_env(_settings(DEEPSEEK_API_KEY="ds", MINIMAX_API_KEY="mm"), store)
    assert store.get_model_preference("chat") is None


def test_seed_skips_when_env_has_nothing(store, monkeypatch):
    """空 env（无 key）→ 什么都不种，空库保持空库。

    注意 seed 的 video 分支走 minimax_keys() 读 os.environ（备用 key 轮换的
    既定设计），所以这里必须显式清掉进程环境里的 MINIMAX_* 才算真"空 env"。
    """
    for var in ("MINIMAX_API_KEY", "MINIMAX_API_KEYS", "MINIMAX_API_KEY_FALLBACKS"):
        monkeypatch.delenv(var, raising=False)
    acts = seed_keys_from_env(_settings(), store)
    assert acts == []
    assert store.list_api_providers() == []


def test_seed_then_chat_is_db_authoritative(store):
    """端到端：种子入库后，chat 从 db 解析（不再是 env）。"""
    seed_keys_from_env(_settings(DEEPSEEK_API_KEY="ds", CHAT_PROVIDER="deepseek"), store)
    cfg = resolve_chat(_settings(DEEPSEEK_API_KEY="ds", CHAT_PROVIDER="deepseek"), store)
    assert cfg.source == "db"
