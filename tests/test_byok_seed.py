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


def test_db_row_beats_env_leftovers(store):
    """DB 里有对话厂商时，env 残留 key 完全不参与（D-48：env 链已删）。"""
    store.create_api_provider(
        kind="llm", name="deepseek", label="DeepSeek",
        base_url="https://api.deepseek.com", api_key="db-key",
        config={"model": "deepseek-v4-pro"},
    )
    # env 里还留着另一家的 key（老配置残留），resolver 必须无视它
    cfg = resolve_chat(_settings(OPENAI_API_KEY="env-oa", CHAT_PROVIDER="openai"), store)
    assert cfg.provider == "deepseek"
    assert cfg.model == "deepseek-v4-pro"
    assert cfg.source == "db"


def test_empty_store_is_unconfigured(store):
    """D-48：DB 空 = 未配置——env 兜底链已删，界面引导用户贴 key。"""
    cfg = resolve_chat(_settings(DEEPSEEK_API_KEY="env-ds", CHAT_PROVIDER="deepseek"), store)
    assert cfg is None


def test_no_store_no_env_returns_none(store):
    assert resolve_chat(_settings(), store=None) is None


# ---------- 种子：只补缺、不覆盖、幂等 ----------


def test_seed_is_idempotent(store, monkeypatch):
    """跑两遍：第二遍什么都不做（不产生重复行）。
    D-48：seed 读 os.environ（一次性迁移语义），测试要显式设环境而不是 Settings。"""
    for var, val in (("DEEPSEEK_API_KEY", "ds"), ("MINIMAX_API_KEY", "mm"),
                     ("ZHIPUAI_API_KEY", "zp")):
        monkeypatch.setenv(var, val)
    s = _settings()
    first = seed_keys_from_env(s, store)
    assert first, "首次应补齐 env 里的配置"
    second = seed_keys_from_env(s, store)
    assert second == [], "第二次必须空跑（幂等）"
    # 每个 (kind,name) 只有一行
    rows = [(r.kind, r.name) for r in store.list_api_providers()]
    assert len(rows) == len(set(rows)), f"出现重复行：{rows}"


def test_seed_never_overwrites_existing_key(store, monkeypatch):
    """已有带 key 的行是用户在 UI 定的，种子绝不覆盖。"""
    store.create_api_provider(
        kind="llm", name="deepseek", label="DeepSeek",
        base_url="https://api.deepseek.com", api_key="user-key",
        config={"model": "deepseek-v4-pro"},
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    seed_keys_from_env(_settings(), store)
    row = next(r for r in store.list_api_providers(kind="llm") if r.name == "deepseek")
    assert row.api_key == "user-key", "种子把用户 key 覆盖成 env 的了"


def test_seed_does_not_set_preference(store, monkeypatch):
    """种子不越俎代庖设默认偏好——多行时由用户在 UI 选，设偏好会盖掉选择。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm")
    seed_keys_from_env(_settings(), store)
    assert store.get_model_preference("chat") is None


def test_seed_skips_when_env_has_nothing(store, monkeypatch):
    """空 env（无 key）→ 什么都不种，空库保持空库。"""
    for var in ("MINIMAX_API_KEY", "MINIMAX_API_KEYS", "MINIMAX_API_KEY_FALLBACKS",
                "OPENAI_API_KEY", "ZHIPUAI_API_KEY", "DEEPSEEK_API_KEY",
                "IMAGE_API_KEY", "TTS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    acts = seed_keys_from_env(_settings(), store)
    assert acts == []
    assert store.list_api_providers() == []


def test_seed_then_chat_is_db_authoritative(store, monkeypatch):
    """端到端：老安装的 env key 经迁移入库后，chat 从 db 解析。

    清掉真实 .env 可能被 bootstrap_env 带进 os.environ 的其他厂商 key——
    seed 会把它们全搬进 DB，行顺序（新行优先）会让断言漂到别的厂商上。"""
    for var in ("OPENAI_API_KEY", "ZHIPUAI_API_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    seed_keys_from_env(_settings(), store)
    cfg = resolve_chat(_settings(), store)
    assert cfg is not None and cfg.source == "db" and cfg.provider == "deepseek"
