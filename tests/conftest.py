# =====================================================================
# conftest.py —— 测试公共夹具
#
# 用 SQLite + 内存 checkpointer 跑测试，无需外部服务。
# =====================================================================

from __future__ import annotations

import os
import time
import warnings
from pathlib import Path

import pytest

# 保证默认走内存 checkpoint，避免测试污染全局单例。
os.environ.setdefault("CHECKPOINTER", "memory")
os.environ.setdefault("OPENAI_API_KEY", "")
# 测试里绝不跑孤儿任务巡检：否则 TestClient 触发 lifespan 时，sweeper 会去连
# 开发库（Postgres）甚至真把任务挂回轮询线程——测试不该碰任何真实服务商。
os.environ.setdefault("VIDEO_SWEEPER_ENABLED", "false")
# 产品主目录隔离（app/home.py）：不钉住的话，测试会读/写/迁移用户真实的
# ~/.wenju（懒迁移会把开发库复制进去）。钉到临时目录，与真实家目录零接触。
import tempfile  # noqa: E402

os.environ.setdefault(
    "WENJU_HOME", str(Path(tempfile.gettempdir()) / f"wenju-test-{os.getpid()}"))

from app.config import Settings  # noqa: E402
from app.llm import LLM  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture()
def tmp_db_path(tmp_path_factory: pytest.TempPathFactory):
    d = Path(__file__).resolve().parent.parent / "data"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"test_{os.getpid()}_{time.time_ns()}.db"
    yield path.as_posix()
    # Windows 上句柄释放有延迟（连接池 + 杀软扫描），unlink 偶尔会失败。
    # 静默吞掉会导致 data/ 慢慢堆满 test_*.db（实测跑几轮就几十个），
    # 所以重试几次，仍然失败就 **出声**——留垃圾可以，但要看得见。
    leftovers = []
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        for attempt in range(3):
            try:
                target.unlink(missing_ok=True)
                break
            except OSError:
                if attempt == 2:
                    leftovers.append(target.name)
                time.sleep(0.05)
    if leftovers:
        warnings.warn(f"临时测试库未能删除（可手动清理 data/）：{leftovers}", stacklevel=1)


@pytest.fixture(scope="session", autouse=True)
def sweep_stale_test_leftovers():
    """会话开始时清扫上一轮没删掉的测试垃圾（data/ 的 workspace_test_* 与 test_*.db）。

    2026-11-12 清理工作区时发现：Windows 句柄延迟让 fixture 末尾的 rmtree/unlink
    偶发失败，几十轮测试在 data/ 和 ~/.wenju/workspace 堆出 100+ 个空目录。
    单靠「失败出声」止不住堆积——本轮先回收上轮的遗骸，目录才能保持干净。
    只认 workspace_test_<pid>_* / test_<pid>_*.db 这两种带进程号指纹的名字，
    不碰正在运行会话的（按 mtime 跳过 1 小时内新建的，避免竞争）。
    """
    import glob
    import shutil
    import time as _t

    d = Path(__file__).resolve().parent.parent / "data"
    roots = [d, Path.home() / ".wenju" / "workspace"]
    for r in roots:
        if not r.is_dir():
            continue
        for p in r.glob("workspace_test_*"):
            try:
                if _t.time() - p.stat().st_mtime < 3600:
                    continue  # 可能是并行会话正在用的
                shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    for db in glob.glob(str(d / "test_*_*.db")):
        try:
            if _t.time() - os.path.getmtime(db) < 3600:
                continue
            for suffix in ("", "-wal", "-shm"):
                Path(db + suffix).unlink(missing_ok=True)
        except OSError:
            pass
    yield


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        DATABASE_URL="sqlite:///./data/_unused.db",
        CHECKPOINTER="memory",
        OPENAI_API_KEY="",
        ZHIPUAI_API_KEY="",
        DEEPSEEK_API_KEY="",
        CHECKPOINT_DSN="",
    )


@pytest.fixture()
def store(tmp_db_path) -> Store:
    db_url = f"sqlite:///{tmp_db_path}"
    s = Store(db_url)
    yield s
    try:
        s.engine.dispose()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def llm(settings) -> LLM:
    return LLM(settings)


@pytest.fixture()
def sample_text() -> str:
    return (
        '凌晨三点，滨江路的路灯在雨里像一团化不开的黄。林然把车停在旧楼前，'
        '熄了火，雨水顺着挡风玻璃往下淌。\n'
        '阿姐坐在里头的一把折叠椅上，说："你终于来了。"林然没答话。\n'
        '角落里站着一个穿红雨衣的男人，把一张照片按在桌上，照片上是失火前夜的'
        '旧货市场。'
    )
