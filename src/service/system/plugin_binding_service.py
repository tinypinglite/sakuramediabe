"""统计/评论单插件绑定的查询与配置写入。"""

from __future__ import annotations

from src.config.config import Settings, settings, update_settings
from src.service.catalog.movie_review_source_service import MovieReviewSourceService
from src.service.catalog.movie_stats_source_service import MovieStatsSourceService


class PluginBindingService:
    @classmethod
    def _stats_sources(cls) -> list[dict]:
        return [
            {
                "plugin_id": plugin_id,
                "display_name": name,
                "display_name_i18n": dict(name_i18n),
                "heat_version": source.heat_version,
                "fields": source.fields,
            }
            for plugin_id, name, name_i18n, source in (
                MovieStatsSourceService.enabled_sources()
            )
        ]

    @classmethod
    def _review_sources(cls) -> list[dict]:
        return [
            {
                "plugin_id": plugin_id,
                "display_name": name,
                "display_name_i18n": dict(name_i18n),
            }
            for plugin_id, name, name_i18n, _source in (
                MovieReviewSourceService.available_sources()
            )
        ]

    @classmethod
    def describe(cls) -> dict:
        stats_sources = cls._stats_sources()
        review_sources = cls._review_sources()
        stats_source = settings.plugins.stats_source
        review_source = settings.plugins.review_source
        stats_entry = next(
            (item for item in stats_sources if item["plugin_id"] == stats_source),
            None,
        )
        review_entry = next(
            (item for item in review_sources if item["plugin_id"] == review_source),
            None,
        )
        fields_by_key = {
            field.key: field for field in MovieStatsSourceService.legacy_fields()
        }
        if stats_entry is not None:
            fields_by_key.update(
                {field.key: field for field in stats_entry["fields"]}
            )
        return {
            "stats_source": stats_source,
            "review_source": review_source,
            "stats_source_available": bool(stats_source and stats_entry),
            "review_source_available": bool(review_source and review_entry),
            "stats_source_display_name": (
                stats_entry["display_name"] if stats_entry else None
            ),
            "stats_source_display_name_i18n": (
                stats_entry["display_name_i18n"] if stats_entry else {}
            ),
            "review_source_display_name": (
                review_entry["display_name"] if review_entry else None
            ),
            "review_source_display_name_i18n": (
                review_entry["display_name_i18n"] if review_entry else {}
            ),
            "stats_fields": list(fields_by_key.values()),
            "heat_version": (
                stats_entry["heat_version"] if stats_entry else None
            ),
            "available_stats_sources": stats_sources,
            "available_review_sources": review_sources,
        }

    @classmethod
    def update(
        cls,
        *,
        stats_source: str | None,
        review_source: str | None,
    ) -> None:
        stats_ids = {item["plugin_id"] for item in cls._stats_sources()}
        review_ids = {item["plugin_id"] for item in cls._review_sources()}
        if stats_source is not None and stats_source not in stats_ids:
            raise ValueError(f"互动数来源不可用: {stats_source}")
        if review_source is not None and review_source not in review_ids:
            raise ValueError(f"评论来源不可用: {review_source}")

        current = Settings.model_validate(settings.model_dump())
        if (
            current.plugins.stats_source == stats_source
            and current.plugins.review_source == review_source
        ):
            return
        current.plugins.stats_source = stats_source
        current.plugins.review_source = review_source
        update_settings(current)
