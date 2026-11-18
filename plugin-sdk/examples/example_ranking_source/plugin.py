"""注册静态排行榜来源和同步任务的示例插件。"""

from __future__ import annotations

import json
from pathlib import Path

from src.plugins import (
    HOST_API_VERSION,
    RANKING_SOURCE_EXTENSION_KEY,
    PluginContext,
    PluginExtension,
    PluginRankingBoard,
    PluginRankingSource,
    PluginRegistration,
)
from src.scheduler.contracts import JobDefinition

from .translations import text, text_map

_PLUGIN_ROOT = Path(__file__).resolve().parent
_MANIFEST = json.loads((_PLUGIN_ROOT / "manifest.json").read_text(encoding="utf-8"))
PLUGIN_ID = _MANIFEST["plugin_id"]
VERSION = _MANIFEST["version"]
DISPLAY_NAME = text("manifest.display_name", "zh-CN", _MANIFEST["display_name"])
SOURCE_KEY = "example_ranking"
BOARD_KEY = "daily"
_MOVIE_NUMBERS = ("EXAMPLE-001", "EXAMPLE-002", "EXAMPLE-003")


def _fetch_numbers(period: str) -> list[str]:
    """返回静态番号列表；列表顺序就是榜单排名。"""
    return list(_MOVIE_NUMBERS)


def register(context: PluginContext) -> PluginRegistration:
    def sync_ranking(reporter, params):
        reporter.emit(
            text=context.i18n.t("jobs.sync.start", default="开始同步示例排行榜")
        )
        return context.sync_ranking_sources(
            progress_callback=reporter.progress_callback,
        )

    source = PluginRankingSource(
        source_key=SOURCE_KEY,
        name=text("ranking.name", "zh-CN", "示例排行榜"),
        name_i18n=text_map("ranking.name"),
        boards=(
            PluginRankingBoard(
                key=BOARD_KEY,
                name=text("ranking.board.daily", "zh-CN", "每日榜"),
                name_i18n=text_map("ranking.board.daily"),
                supported_periods=("daily",),
                default_period="daily",
                fetch_numbers=_fetch_numbers,
            ),
        ),
    )
    return PluginRegistration(
        plugin_id=PLUGIN_ID,
        display_name=DISPLAY_NAME,
        display_name_i18n=text_map("manifest.display_name"),
        version=VERSION,
        host_api_version=HOST_API_VERSION,
        extensions=(
            PluginExtension(key=RANKING_SOURCE_EXTENSION_KEY, data=source),
        ),
        jobs=(
            JobDefinition(
                task_key="example_ranking_source_sync",
                log_name="example-ranking-source-sync",
                cli_name="example-ranking-source-sync",
                cli_help="同步示例排行榜",
                cli_help_i18n=text_map("jobs.sync.cli_help"),
                default_cron="0 4 * * *",
                handler=sync_ranking,
            ),
        ),
    )
