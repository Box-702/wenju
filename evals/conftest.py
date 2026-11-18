"""evals 的 pytest 配置：在线层默认关闭。

L1/L2 每条 case 都要真调模型（11 条 case × 导演/美术/摄影 3 次，judge 再加
最多 3 次），一次全量约 100+ 次调用、几十分钟。这是「改完提示词主动验一次」
的付费动作，不该混在 `pytest evals` 的默认路径里——T-18 与 5.70 各误跑过一次。

默认只跑离线层（24 条、1–2 秒、不花钱）；要在线层必须显式 `--online`。
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--online",
        action="store_true",
        default=False,
        help="跑 L1/L2 在线层（真实调模型、要花钱）；默认只跑离线层",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--online"):
        return
    skip = pytest.mark.skip(reason="在线层默认关闭（真实调模型、花钱）：需要时加 --online")
    for item in items:
        if "online" in item.keywords:
            item.add_marker(skip)
