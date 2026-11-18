"""注册评论来源和互动数来源的示例插件。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.plugins import (
    HOST_API_VERSION,
    REVIEWS_EXTENSION_KEY,
    STATS_SOURCE_EXTENSION_KEY,
    PluginContext,
    PluginExtension,
    PluginMovieReview,
    PluginMovieReviewSource,
    PluginMovieStatsField,
    PluginMovieStatsSnapshot,
    PluginMovieStatsSource,
    PluginRegistration,
)

from .translations import text, text_map

_PLUGIN_ROOT = Path(__file__).resolve().parent
_MANIFEST = json.loads((_PLUGIN_ROOT / "manifest.json").read_text(encoding="utf-8"))
PLUGIN_ID = _MANIFEST["plugin_id"]
VERSION = _MANIFEST["version"]
DISPLAY_NAME = text("manifest.display_name", "zh-CN", _MANIFEST["display_name"])
TEST_MOVIE_NUMBER = "EXAMPLE-001"


def register(context: PluginContext) -> PluginRegistration:
    def fetch_reviews(
        movie_number: str,
        page: int,
        page_size: int,
        sort: str,
    ) -> list[PluginMovieReview] | None:
        if movie_number.strip().upper() != TEST_MOVIE_NUMBER:
            return None
        if page > 1:
            return []
        content = context.i18n.t(
            "reviews.content",
            default="示例评论：这是一条静态评论。",
        )
        reviews = [
            PluginMovieReview(
                id=1,
                score=9,
                content=content,
                created_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
                username="示例用户",
                like_count=12,
                watch_count=3,
            ),
            PluginMovieReview(
                id=2,
                score=7,
                content=content,
                created_at=datetime(2026, 1, 3, 4, 5, tzinfo=timezone.utc),
                username="另一位示例用户",
                like_count=4,
                watch_count=1,
            ),
        ]
        return reviews[:page_size]

    def fetch_stats(movie_number: str) -> PluginMovieStatsSnapshot | None:
        if movie_number.strip().upper() != TEST_MOVIE_NUMBER:
            return None
        likes = 12
        views = 345
        return PluginMovieStatsSnapshot(
            values={"likes": likes, "views": views},
            heat=likes * 3 + views,
        )

    return PluginRegistration(
        plugin_id=PLUGIN_ID,
        display_name=DISPLAY_NAME,
        display_name_i18n=text_map("manifest.display_name"),
        version=VERSION,
        host_api_version=HOST_API_VERSION,
        test_movie_number=TEST_MOVIE_NUMBER,
        extensions=(
            PluginExtension(
                key=REVIEWS_EXTENSION_KEY,
                data=PluginMovieReviewSource(fetch_reviews=fetch_reviews),
            ),
            PluginExtension(
                key=STATS_SOURCE_EXTENSION_KEY,
                data=PluginMovieStatsSource(
                    fields=[
                        PluginMovieStatsField(
                            key="likes",
                            value_type="int",
                            label_i18n=text_map("stats.likes"),
                            min=0,
                        ),
                        PluginMovieStatsField(
                            key="views",
                            value_type="int",
                            label_i18n=text_map("stats.views"),
                            min=0,
                        ),
                    ],
                    heat_version="1",
                    fetch_stats=fetch_stats,
                ),
            ),
        ),
    )
