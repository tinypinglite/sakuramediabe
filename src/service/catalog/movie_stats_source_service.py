"""互动数来源注册表与单插件绑定。"""

from __future__ import annotations

from src.config.config import settings
from src.i18n import SUPPORTED_LANGUAGES, translate
from src.plugins.extensions.stats import (
    STATS_SOURCE_EXTENSION_KEY,
    PluginMovieStatsField,
    PluginMovieStatsSource,
    validate_stats_extension,
)

LEGACY_STATS_SOURCE = "legacy:host"
LEGACY_HEAT_VERSION = "host:v6"

_LEGACY_FIELD_SPECS = (
    ("score", "float", "stats.fields.score", None),
    ("score_number", "int", "stats.fields.score_number", None),
    ("watched_count", "int", "stats.fields.watched_count", None),
    ("want_watch_count", "int", "stats.fields.want_watch_count", None),
    ("comment_count", "int", "stats.fields.comment_count", None),
)


class MovieStatsSourceUnavailable(RuntimeError):
    pass


class MovieStatsSourceService:
    sources = ()

    @classmethod
    def register(cls, registrations):
        cls.sources = tuple(
            (
                registration.plugin_id,
                registration.display_name,
                registration.display_name_i18n,
                validate_stats_extension(
                    plugin_id=registration.plugin_id, extension=extension
                ),
            )
            for registration in registrations
            for extension in registration.extensions
            if extension.key == STATS_SOURCE_EXTENSION_KEY
        )

    @classmethod
    def enabled_sources(cls):
        sources = {
            plugin_id: (name, name_i18n, source)
            for plugin_id, name, name_i18n, source in cls.sources
        }
        return tuple(
            (plugin_id, *sources[plugin_id])
            for plugin_id in settings.plugins.enabled
            if plugin_id in sources
        )

    @classmethod
    def bound_source(cls) -> tuple[str, PluginMovieStatsSource]:
        plugin_id = settings.plugins.stats_source
        if not plugin_id:
            raise MovieStatsSourceUnavailable("未绑定互动数来源")
        entry = next(
            (
                source
                for current_id, _name, _name_i18n, source in cls.enabled_sources()
                if current_id == plugin_id
            ),
            None,
        )
        if entry is None:
            raise MovieStatsSourceUnavailable(
                f"绑定的互动数来源不可用: {plugin_id}"
            )
        return plugin_id, entry

    @classmethod
    def describe_bound_source(cls) -> dict | None:
        try:
            plugin_id, source = cls.bound_source()
        except MovieStatsSourceUnavailable:
            return None
        entry = next(
            (
                (name, name_i18n)
                for current_id, name, name_i18n, _source in cls.enabled_sources()
                if current_id == plugin_id
            ),
            None,
        )
        if entry is None:
            return None
        return {
            "plugin_id": plugin_id,
            "display_name": entry[0],
            "display_name_i18n": entry[1],
            "heat_version": source.heat_version,
            "fields": source.fields,
        }

    @classmethod
    def legacy_fields(cls) -> list[PluginMovieStatsField]:
        return [
            PluginMovieStatsField(
                key=key,
                value_type=value_type,
                label_i18n={
                    locale: translate(label_key, locale=locale)
                    for locale in SUPPORTED_LANGUAGES
                },
                max=max_value,
            )
            for key, value_type, label_key, max_value in _LEGACY_FIELD_SPECS
        ]

