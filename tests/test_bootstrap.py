# =====================================================================
# test_bootstrap.py —— 进程启动引导
#
# 这个测试锁的是一条容易回归的性质：**有一批配置不走 Settings，而是直接读
# os.environ**。只让 Settings 读到 .env 是不够的——那会造成「配置存在但代码
# 看不见」，而且完全没有报错。
#
# 实测症状：CLI / 脚本里 `minimax_keys()` 返回 0 把，备用 key 全部失效。
# =====================================================================

from __future__ import annotations

import os

import pytest

from app.config import bootstrap_env, get_settings
from app.keys import _cooldown_setting, minimax_keys, reset_rings

_DIRECT_ENV_KEYS = (
    "MINIMAX_API_KEY",
    "MINIMAX_API_KEYS",
    "MINIMAX_API_KEY_FALLBACKS",
    "KEY_COOLDOWN_SEC",
)


@pytest.fixture()
def clean_env(monkeypatch):
    """清掉直读环境变量的这几个键（.env 可能真的配了，测试要可复现）。"""
    for name in _DIRECT_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    reset_rings()
    yield
    reset_rings()


def test_bootstrap_calls_load_dotenv(clean_env, monkeypatch):
    """bootstrap 必须真的调 load_dotenv，并且用项目根的 .env。

    这条不依赖本机 .env 是否存在——CI 上没有 .env 也要绿。
    """
    import app.config as cfg

    calls: list[object] = []

    def fake_load(path=None, *a, **kw):
        calls.append(path)
        return True

    monkeypatch.setattr("dotenv.load_dotenv", fake_load)
    bootstrap_env()
    assert calls, "bootstrap_env 没有调用 load_dotenv"


def test_bootstrap_wires_env_into_direct_readers(clean_env, monkeypatch):
    """bootstrap 之后，直读 os.environ 的代码能看到 .env 里的值。

    这是修复「CLI 下备用 key 全部失效」的判据。用假的 load_dotenv 注入，
    保证与开发者本机的 .env 无关。
    """
    def fake_load(path=None, *a, **kw):
        monkeypatch.setenv("MINIMAX_API_KEY_FALLBACKS", "fb1,fb2")
        monkeypatch.setenv("KEY_COOLDOWN_SEC", "77")
        return True

    monkeypatch.setattr("dotenv.load_dotenv", fake_load)
    assert minimax_keys() == []

    bootstrap_env()

    assert minimax_keys() == ["fb1", "fb2"]
    assert _cooldown_setting() == 77.0


def test_bootstrap_is_idempotent(clean_env):
    """入口可能被多次导入，重复调用不能出问题。"""
    a = bootstrap_env()
    b = bootstrap_env()
    assert a is b or a.database_url == b.database_url
    first = minimax_keys()
    bootstrap_env()
    assert minimax_keys() == first


def test_bootstrap_does_not_override_existing_env(clean_env, monkeypatch):
    """已显式设置的变量优先级更高（测试用 monkeypatch 注入的不能被 .env 盖掉）。

    D-48 收口：MINIMAX_API_KEY 已退役——env 里的主 key 不再进候选
    （主 key 只认 DB），FALLBACKS 安全网仍是直读 environ 的合法入口。
    """
    monkeypatch.setenv("MINIMAX_API_KEY", "retired-primary")
    monkeypatch.setenv("MINIMAX_API_KEY_FALLBACKS", "explicit-fb")
    bootstrap_env()
    assert "retired-primary" not in minimax_keys()
    assert "explicit-fb" in minimax_keys()


def test_settings_still_reads_env_file_independently(clean_env):
    """Settings 自己读 .env——这条是既有能力，别在修 bootstrap 时弄丢。"""
    s = get_settings()
    assert s.database_url                 # 有默认值
    assert isinstance(s.output_language, str)


def test_cooldown_setting_reads_environment(clean_env, monkeypatch):
    """KEY_COOLDOWN_SEC 是直读 environ 的，设了就要生效。"""
    monkeypatch.setenv("KEY_COOLDOWN_SEC", "42")
    assert _cooldown_setting() == 42.0


def test_entry_points_bootstrap():
    """两个真正的入口都必须调用 bootstrap_env——否则配置对它们不可见。"""
    import pathlib

    for path in ("app/main.py", "app/cli.py"):
        src = pathlib.Path(path).read_text(encoding="utf-8")
        assert "bootstrap_env" in src, f"{path} 没有调用 bootstrap_env()，直读 environ 的配置会失效"
