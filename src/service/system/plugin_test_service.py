"""插件能力测试：按插件自己的测试番号调用元数据、互动数与评论扩展点。"""

from __future__ import annotations

import time
from collections.abc import Sequence

from loguru import logger

from src.metadata.provider import MetadataNotFoundError
from src.plugins.contracts import PluginRegistration
from src.plugins.extensions.metadata import METADATA_SOURCE_EXTENSION_KEY
from src.plugins.extensions.reviews import (
    REVIEWS_EXTENSION_KEY,
    PluginMovieReview,
    validate_reviews_extension,
)
from src.plugins.extensions.stats import (
    STATS_SOURCE_EXTENSION_KEY,
    PluginMovieStatsSnapshot,
    validate_stats_extension,
    validate_stats_snapshot,
)
from src.schema.system.plugins import (
    PluginTestCapability,
    PluginTestMetadataSummary,
    PluginTestResultResource,
    PluginTestReviewsSummary,
    PluginTestStatsSummary,
)
from src.service.catalog.metadata_source_service import MetadataSourceService

DEFAULT_TEST_MOVIE_NUMBER = "SSIS-001"
_CAPABILITY_EXTENSION_KEYS = {
    "metadata": METADATA_SOURCE_EXTENSION_KEY,
    "stats": STATS_SOURCE_EXTENSION_KEY,
    "reviews": REVIEWS_EXTENSION_KEY,
}
_CAPABILITIES: tuple[PluginTestCapability, ...] = ("metadata", "stats", "reviews")


class PluginTestUnavailable(RuntimeError):
    pass


class PluginTestCapabilityUnsupported(RuntimeError):
    pass


class PluginTestService:
    registrations: tuple[PluginRegistration, ...] = ()

    @classmethod
    def register(cls, registrations: Sequence[PluginRegistration]) -> None:
        cls.registrations = tuple(registrations)

    @classmethod
    def _registration(cls, plugin_id: str) -> PluginRegistration:
        registration = next(
            (
                item
                for item in cls.registrations
                if item.plugin_id == plugin_id
            ),
            None,
        )
        if registration is None:
            raise PluginTestUnavailable(f"插件未加载: {plugin_id}")
        return registration

    @classmethod
    def _capabilities(cls, registration: PluginRegistration) -> tuple[str, ...]:
        extension_keys = {extension.key for extension in registration.extensions}
        return tuple(
            capability
            for capability in _CAPABILITIES
            if _CAPABILITY_EXTENSION_KEYS[capability] in extension_keys
        )

    @classmethod
    def describe(cls, plugin_id: str) -> tuple[tuple[str, ...], str | None]:
        try:
            registration = cls._registration(plugin_id)
        except PluginTestUnavailable:
            return (), None
        capabilities = cls._capabilities(registration)
        if not capabilities:
            return (), None
        return capabilities, (
            registration.test_movie_number or DEFAULT_TEST_MOVIE_NUMBER
        )

    @classmethod
    def run(
        cls,
        plugin_id: str,
        capability: PluginTestCapability,
    ) -> PluginTestResultResource:
        registration = cls._registration(plugin_id)
        if capability not in cls._capabilities(registration):
            raise PluginTestCapabilityUnsupported(
                f"插件未声明能力: {capability}"
            )
        movie_number = (
            registration.test_movie_number or DEFAULT_TEST_MOVIE_NUMBER
        )
        started = time.perf_counter()
        try:
            result = cls._run_capability(
                registration,
                capability=capability,
                movie_number=movie_number,
            )
        except MetadataNotFoundError:
            result = PluginTestResultResource(
                capability=capability,
                status="empty",
                movie_number=movie_number,
                duration_ms=0,
            )
        except Exception as exc:
            logger.warning(
                "Plugin test failed plugin={} capability={} detail={}",
                plugin_id,
                capability,
                exc,
            )
            result = PluginTestResultResource(
                capability=capability,
                status="failed",
                movie_number=movie_number,
                duration_ms=0,
                error_type=type(exc).__name__,
                error_message=cls._error_message(exc),
            )
        return result.model_copy(
            update={
                "duration_ms": round((time.perf_counter() - started) * 1000)
            }
        )

    @classmethod
    def _run_capability(
        cls,
        registration: PluginRegistration,
        *,
        capability: PluginTestCapability,
        movie_number: str,
    ) -> PluginTestResultResource:
        if capability == "metadata":
            return cls._test_metadata(
                registration,
                movie_number=movie_number,
            )
        if capability == "stats":
            return cls._test_stats(
                registration,
                movie_number=movie_number,
            )
        return cls._test_reviews(
            registration,
            movie_number=movie_number,
        )

    @classmethod
    def _test_metadata(
        cls,
        registration: PluginRegistration,
        *,
        movie_number: str,
    ) -> PluginTestResultResource:
        with MetadataSourceService.fetch_plugin(
            registration.plugin_id, movie_number
        ) as (detail, _source):
            return PluginTestResultResource(
                capability="metadata",
                status="ok",
                movie_number=movie_number,
                duration_ms=0,
                metadata=PluginTestMetadataSummary(
                    movie_number=detail.movie_number,
                    title=detail.title,
                    release_date=detail.release_date,
                    duration_minutes=detail.duration_minutes,
                    actor_count=len(detail.actors),
                    tag_count=len(detail.tags),
                    image_count=1 + len(detail.plot_image_paths),
                    source_url=detail.source_url,
                ),
            )

    @classmethod
    def _test_stats(
        cls,
        registration: PluginRegistration,
        *,
        movie_number: str,
    ) -> PluginTestResultResource:
        extension = next(
            (
                extension
                for extension in registration.extensions
                if extension.key == STATS_SOURCE_EXTENSION_KEY
            ),
            None,
        )
        if extension is None:
            raise PluginTestCapabilityUnsupported("插件未声明互动数能力")
        source = validate_stats_extension(
            plugin_id=registration.plugin_id,
            extension=extension,
        )
        result = source.fetch_stats(movie_number)
        if result is None:
            return PluginTestResultResource(
                capability="stats",
                status="empty",
                movie_number=movie_number,
                duration_ms=0,
            )
        snapshot = PluginMovieStatsSnapshot.model_validate(result)
        validate_stats_snapshot(source, snapshot)
        return PluginTestResultResource(
            capability="stats",
            status="ok",
            movie_number=movie_number,
            duration_ms=0,
            stats=PluginTestStatsSummary(
                heat=snapshot.heat,
                field_count=len(snapshot.values),
                values=dict(snapshot.values),
            ),
        )

    @classmethod
    def _test_reviews(
        cls,
        registration: PluginRegistration,
        *,
        movie_number: str,
    ) -> PluginTestResultResource:
        extension = next(
            (
                extension
                for extension in registration.extensions
                if extension.key == REVIEWS_EXTENSION_KEY
            ),
            None,
        )
        if extension is None:
            raise PluginTestCapabilityUnsupported("插件未声明评论能力")
        source = validate_reviews_extension(
            plugin_id=registration.plugin_id,
            extension=extension,
        )
        result = source.fetch_reviews(movie_number, 1, 5, "recently")
        if result is None:
            return PluginTestResultResource(
                capability="reviews",
                status="empty",
                movie_number=movie_number,
                duration_ms=0,
            )
        reviews = [
            PluginMovieReview.model_validate(item)
            for item in result
        ]
        if not reviews:
            return PluginTestResultResource(
                capability="reviews",
                status="empty",
                movie_number=movie_number,
                duration_ms=0,
            )
        first = reviews[0]
        return PluginTestResultResource(
            capability="reviews",
            status="ok",
            movie_number=movie_number,
            duration_ms=0,
            reviews=PluginTestReviewsSummary(
                item_count=len(reviews),
                first_username=first.username or None,
                first_excerpt=(first.content.strip()[:120] or None),
            ),
        )

    @staticmethod
    def _error_message(exc: Exception) -> str:
        message = str(exc).strip()
        return message[:600] if message else type(exc).__name__
