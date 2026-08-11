"""SakuraMedia 插件基础设施（v2）。"""

from __future__ import annotations

import importlib
from typing import Any

_PUBLIC_EXPORTS = {
    "HOST_API_VERSION": "src.plugins.contracts",
    "MIN_SUPPORTED_HOST_API_VERSION": "src.plugins.contracts",
    "PluginRegistration": "src.plugins.contracts",
    "PluginContext": "src.plugins.context",
}

__all__ = [
    "HOST_API_VERSION",
    "MIN_SUPPORTED_HOST_API_VERSION",
    "PluginContext",
    "PluginRegistration",
]


def __getattr__(name: str) -> Any:
    """懒导出公开契约，避免顶层导入把 scheduler/config 拉成循环依赖。"""
    module_name = _PUBLIC_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
