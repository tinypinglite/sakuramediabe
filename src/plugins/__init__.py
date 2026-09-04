"""SakuraMedia 插件基础设施。"""

from __future__ import annotations

import importlib
from typing import Any

_PUBLIC_EXPORTS = {
    "ACTOR_SNAPSHOT_FIELDS": "src.plugins.types",
    "ActorSnapshot": "src.plugins.types",
    "ActorPage": "src.plugins.types",
    "HOST_API_VERSION": "src.plugins.contracts",
    "MIN_SUPPORTED_HOST_API_VERSION": "src.plugins.contracts",
    "MOVIE_SNAPSHOT_FIELDS": "src.plugins.types",
    "MovieSnapshot": "src.plugins.types",
    "MoviePage": "src.plugins.types",
    "TagSnapshot": "src.plugins.types",
    "PluginRegistration": "src.plugins.contracts",
    "PluginExtension": "src.plugins.contracts",
    "PluginContext": "src.plugins.context",
    "PluginRankingBoard": "src.plugins.extensions.ranking",
    "PluginRankingSource": "src.plugins.extensions.ranking",
    "RANKING_SOURCE_EXTENSION_KEY": "src.plugins.extensions.ranking",
}

__all__ = [
    "ACTOR_SNAPSHOT_FIELDS",
    "HOST_API_VERSION",
    "MIN_SUPPORTED_HOST_API_VERSION",
    "MOVIE_SNAPSHOT_FIELDS",
    "RANKING_SOURCE_EXTENSION_KEY",
    "ActorPage",
    "ActorSnapshot",
    "MoviePage",
    "MovieSnapshot",
    "PluginContext",
    "PluginExtension",
    "PluginRankingBoard",
    "PluginRankingSource",
    "PluginRegistration",
    "TagSnapshot",
]


def __getattr__(name: str) -> Any:
    """懒导出公开契约，避免顶层导入把 scheduler/config 拉成循环依赖。"""
    module_name = _PUBLIC_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
