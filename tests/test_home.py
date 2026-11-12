# =====================================================================
# test_home.py —— 产品主目录（~/.wenju）与懒迁移
#
# 全部用 tmp_path 当 WENJU_HOME / 假项目根，绝不碰真实家目录。
# =====================================================================

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import home


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    h = tmp_path / "wenju-home"
    monkeypatch.setenv(home._HOME_ENV, str(h))
    return h


@pytest.fixture()
def fake_project(tmp_path):
    p = tmp_path / "proj"
    (p / "data").mkdir(parents=True)
    return p


def test_骨架建全(fake_home):
    h = home.ensure_home()
    assert h == fake_home
    assert (h / "workspace").is_dir() and (h / "runs").is_dir()


def test_幂等再入(fake_home):
    home.ensure_home()
    (fake_home / "app.db").write_bytes(b"x")
    home.ensure_home()  # 第二次不得动已有库
    assert (fake_home / "app.db").read_bytes() == b"x"


def test_懒迁移整目录复制(fake_home, fake_project):
    db = fake_project / "data" / "script_agent.db"
    db.write_bytes(b"MAIN")
    (db.parent / "孔乙己").mkdir()
    (db.parent / "孔乙己" / "01_原稿.txt").write_text("稿", encoding="utf-8")
    # 调试残留不得被误认成主库
    (db.parent / "verify.db").write_bytes(b"junk")

    home.ensure_home(fake_project)
    assert (fake_home / "app.db").read_bytes() == b"MAIN"
    # 工作区剧名文件夹搬进 workspace/
    assert (fake_home / "workspace" / "孔乙己" / "01_原稿.txt").exists()
    # 旧库原样保留（可回退）
    assert db.exists()


def test_家目录已有库绝不覆盖(fake_home, fake_project):
    (fake_project / "data" / "script_agent.db").write_bytes(b"OLD")
    (fake_home).mkdir(parents=True)
    (fake_home / "app.db").write_bytes(b"NEWER")
    home.ensure_home(fake_project)
    assert (fake_home / "app.db").read_bytes() == b"NEWER"


def test_无旧库不报错(fake_home):
    h = home.ensure_home(Path("Z:/nonexistent-project-xyz"))
    assert h.is_dir() and not (h / "app.db").exists()
