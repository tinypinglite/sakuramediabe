"""cloud115 集成测试门禁：--run-cloud115-integration flag。

默认所有带 cloud115_integration marker 的用例都会被 skip，避免 CI 打真实 115 端点。
本地手动跑：
    uv run pytest tests/lib/cloud115/test_integration.py --run-cloud115-integration -n0 -v
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-cloud115-integration",
        action="store_true",
        default=False,
        help="run cloud115 integration tests that hit the real 115 API (needs COOKIE_115 env var)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # 未显式加 flag 时，把所有带 marker 的用例批量 skip
    if config.getoption("--run-cloud115-integration"):
        return
    skip_marker = pytest.mark.skip(reason="need --run-cloud115-integration flag")
    for item in items:
        if "cloud115_integration" in item.keywords:
            item.add_marker(skip_marker)
