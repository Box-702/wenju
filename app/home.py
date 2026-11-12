# =====================================================================
# home.py —— 产品主目录（仿 Codex 的 ~/.codex）：用户数据跟着用户走
#
#   ~/.wenju/                    ← 可用 WENJU_HOME 环境变量整体覆盖
#     app.db                     元数据权威（SQLite）
#     workspace/                 作品工作区（每剧一个文件夹，见 workspace.py）
#       <剧名>/01_原稿 … 07_成片
#     runs/                      工作流直跑（N2D）每次一夹：日志 + 产物快照
#
#   为什么：数据曾躺在项目 data/ 里，跟着代码目录走——项目被复制/搬迁时
#   作品会「消失」（2026-11-12 实际踩过：机器上两份项目文件夹互相认错）。
#   本地 Agent 的正解是代码与数据分离：备份 = 拷一个家目录，换 clone 目录
#   不丢作品。
#
#   懒迁移：ensure_home() 在家目录还没有 app.db、而项目 data/ 有旧库时，
#   把旧库与工作区文件**复制**进来（旧目录原样保留，出问题可回退）。
#   只复制不删除、只在缺失时复制——在跑的进程不受影响，下次启动自然完成搬家。
# =====================================================================

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_HOME_ENV = "WENJU_HOME"
# 测试必须与真实家目录隔离：pytest 里设了 WENJU_HOME_TESTS 就走临时目录。
_TESTS_ENV = "PYTEST_CURRENT_TEST"


def home_root() -> Path:
    """产品主目录（不保证存在；用 ensure_home 建）。"""
    v = os.environ.get(_HOME_ENV, "").strip()
    if v:
        return Path(v).expanduser()
    return Path.home() / ".wenju"


def workspace_root() -> Path:
    return home_root() / "workspace"


def runs_root() -> Path:
    return home_root() / "runs"


def db_path() -> Path:
    return home_root() / "app.db"


def ensure_home(project_root: Path | None = None) -> Path:
    """建好家目录骨架，并在需要时从项目 data/ 懒迁移旧数据。返回家目录。

    幂等、廉价（纯 stat + 至多一次复制），每个进程入口调用一次即可。
    迁移条件严格：家目录没有 app.db 且项目 data/ 有 *.db——两个条件差一个
    都不动，绝不覆盖已有库。
    """
    h = home_root()
    h.mkdir(parents=True, exist_ok=True)
    workspace_root().mkdir(parents=True, exist_ok=True)
    runs_root().mkdir(parents=True, exist_ok=True)

    if not db_path().exists():
        legacy = _legacy_db(project_root)
        if legacy is not None:
            try:
                shutil.copy2(legacy, db_path())
                # WAL/SHM 附属文件一起搬：旧库可能有未合并的写前日志。
                for suffix in ("-wal", "-shm"):
                    extra = Path(str(legacy) + suffix)
                    if extra.exists():
                        shutil.copy2(extra, Path(str(db_path()) + suffix))
                log.info("已把旧数据库迁入产品主目录：%s → %s（旧库保留未动）",
                         legacy, db_path())
                migrate_workspace_files(project_root)
            except OSError as e:
                log.warning("数据库迁移失败（本次用家目录新库起步）：%s", e)
    return h


def _legacy_db(project_root: Path | None) -> Path | None:
    """项目 data/ 下的主库：只认 script_agent.db / app.db 这类正名文件。

    刻意不做「挑最大」的启发式：data/ 里常年散落 verify.db、*_e2e.db 等
    调试残留，误认主库会把垃圾搬进家目录并从此挡住真迁移。
    名字对不上时宁可迁错方向不迁——不迁（新库起步），用户可手动放文件。"""
    root = project_root or Path(__file__).resolve().parent.parent
    d = root / "data"
    for name in ("script_agent.db", "app.db", "wenju.db"):
        p = d / name
        try:
            if p.exists() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


def migrate_workspace_files(project_root: Path | None) -> None:
    """把项目 data/ 里的旧工作区子目录（剧名文件夹）复制进家目录 workspace/。

    只复制目录、跳过 *.db / _chat*.json 等散件；目标已存在同名目录则跳过
    （搬家只补空缺，不覆盖）。"""
    if project_root is None:
        return
    src = Path(project_root) / "data"
    if not src.is_dir():
        return
    dst = workspace_root()
    for item in src.iterdir():
        if not item.is_dir() or item.name.startswith((".", "__")):
            continue
        target = dst / item.name
        if target.exists():
            continue
        try:
            shutil.copytree(item, target)
            log.info("工作区已迁入：%s", target)
        except OSError as e:
            log.warning("工作区迁移跳过 %s：%s", item.name, e)
