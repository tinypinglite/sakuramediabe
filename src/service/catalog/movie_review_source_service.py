"""按启用插件顺序获取电影评论。"""

from __future__ import annotations

from loguru import logger

from src.config.config import settings
from src.i18n.text import resolve_text
from src.plugins.extensions.reviews import (
    REVIEWS_EXTENSION_KEY,
    PluginMovieReview,
    validate_reviews_extension,
)


class MovieReviewsUnavailable(RuntimeError):
    pass


class MovieReviewSourceService:
    sources = ()

    @classmethod
    def register(cls, registrations):
        cls.sources = tuple(
            (
                registration.plugin_id,
                registration.display_name,
                registration.display_name_i18n,
                validate_reviews_extension(
                    plugin_id=registration.plugin_id, extension=extension
                ),
            )
            for registration in registrations
            for extension in registration.extensions
            if extension.key == REVIEWS_EXTENSION_KEY
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
    def bound_source(cls):
        plugin_id = settings.plugins.review_source
        if not plugin_id:
            return None
        for entry in cls.enabled_sources():
            if entry[0] == plugin_id:
                return entry
        raise MovieReviewsUnavailable(
            f"绑定的评论来源不可用: {plugin_id}"
        )

    @classmethod
    def available_sources(cls):
        return cls.enabled_sources()

    @classmethod
    def fetch(
        cls,
        movie_number: str,
        *,
        page: int,
        page_size: int,
        sort: str,
    ) -> list[PluginMovieReview]:
        bound = cls.bound_source()
        if bound is None:
            return []
        plugin_id, name, name_i18n, source = bound
        display_name = resolve_text(name, name_i18n)
        try:
            result = source.fetch_reviews(movie_number, page, page_size, sort)
        except Exception as exc:
            logger.warning(
                "Movie review source failed plugin={} movie_number={} detail={}",
                plugin_id,
                movie_number,
                exc,
            )
            raise MovieReviewsUnavailable(
                f"{display_name}: {type(exc).__name__}"
            ) from exc
        if result is None:
            return []
        return [PluginMovieReview.model_validate(item) for item in result]
