from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from peewee import fn

from src.common.database import ensure_database_ready
from src.common.runtime_time import utc_now_for_db
from src.model import Media, MediaLibrary, MediaThumbnail
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
    ProviderUnavailableError,
    ThumbnailBackendUnavailable,
    ThumbnailGenerationDeferred,
)
from src.service.playback.provider_helpers import media_handle_for
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.contracts import ThumbnailDeferred


@dataclass(frozen=True)
class ThumbnailGenerationOutcome:
    state: str
    generated_count: int = 0
    error_code: str | None = None


class MediaThumbnailTaskService:
    """Generate one complete thumbnail set per Media through its provider."""

    TASK_KEY = "media_thumbnail_generation"
    MAX_FAILURE_ATTEMPTS = 2
    MAX_DEFERRED_ATTEMPTS = 3
    DEFERRED_BACKOFF_BASE_SECONDS = 15 * 60
    FAILURE_RETRY_BACKOFF_BASE_SECONDS = 15 * 60
    FAILURE_RETRY_BACKOFF_MAX_SECONDS = 24 * 60 * 60
    TERMINAL_ERROR_CODES = frozenset(
        {
            "thumbnail_generation_empty",
            "thumbnail_generation_insufficient_count",
            "thumbnail_generation_unparseable_filenames",
            "thumbnail_offset_invalid",
            "thumbnail_artifact_empty",
            "thumbnail_artifact_not_webp",
            "thumbnail_artifact_invalid",
        }
    )

    @staticmethod
    def _thumbnail_exists_query():
        return MediaThumbnail.select(MediaThumbnail.id).where(MediaThumbnail.media == Media.id)

    @classmethod
    def _missing_thumbnail_condition(cls):
        return ~fn.EXISTS(cls._thumbnail_exists_query())

    @classmethod
    def _candidate_query(cls):
        now = utc_now_for_db()
        normal_state = (
            Media.thumbnail_generation_state.in_(
                (Media.THUMBNAIL_STATE_PENDING, Media.THUMBNAIL_STATE_SUCCEEDED)
            )
            | (
                (Media.thumbnail_generation_state == Media.THUMBNAIL_STATE_RETRY_WAIT)
                & (
                    Media.thumbnail_next_retry_at.is_null(True)
                    | (Media.thumbnail_next_retry_at <= now)
                )
            )
        )
        return (
            Media.select(Media.id)
            .join(MediaLibrary)
            .where(Media.valid == True, cls._missing_thumbnail_condition(), normal_state)
            .order_by(Media.id)
        )

    @classmethod
    def _candidate_entries(cls) -> list[tuple[int, tuple[str, int]]]:
        return [
            (int(media_id), (str(provider_key), int(library_id)))
            for media_id, provider_key, library_id in (
                cls._candidate_query()
                .select(Media.id, MediaLibrary.provider_key, MediaLibrary.id)
                .tuples()
            )
        ]

    @classmethod
    def _count_state(cls, state: str) -> int:
        return (
            Media.select(Media.id)
            .where(
                cls._missing_thumbnail_condition(),
                Media.thumbnail_generation_state == state,
            )
            .count()
        )

    @classmethod
    def count_pending_media(cls) -> int:
        return cls._candidate_query().count()

    @classmethod
    def count_retry_wait_media(cls) -> int:
        return cls._count_state(Media.THUMBNAIL_STATE_RETRY_WAIT)

    @classmethod
    def count_terminal_failed_media(cls) -> int:
        return cls._count_state(Media.THUMBNAIL_STATE_TERMINAL)

    @staticmethod
    def minimum_acceptable_count(expected_count: int) -> int:
        return max(1, int(expected_count * 0.85))

    @classmethod
    def _generate_artifacts(cls, media: Media) -> int:
        handle = media_handle_for(media)
        with tempfile.TemporaryDirectory(prefix=f"media-thumbnails-{media.id}-") as workspace_name:
            workspace = Path(workspace_name)
            try:
                storage = MEDIA_PROVIDER_REGISTRY.storage_for(handle.library)
                generation = storage.generate_thumbnails(media=handle, workspace=workspace)
            except ThumbnailGenerationDeferred as exc:
                raise ThumbnailDeferred(
                    str(exc),
                    error_code=exc.error_code,
                    max_deferred_attempts=exc.max_deferred_attempts,
                    deferred_backoff_base_seconds=exc.deferred_backoff_base_seconds,
                ) from exc
            except ProviderOperationError as exc:
                if exc.code != "unavailable" or not exc.retryable:
                    raise
                raise ThumbnailDeferred(
                    "媒体提供方暂不可用",
                    error_code=exc.code,
                    max_deferred_attempts=cls.MAX_DEFERRED_ATTEMPTS,
                    deferred_backoff_base_seconds=cls.DEFERRED_BACKOFF_BASE_SECONDS,
                ) from exc
            expected_count = int(generation.expected_count)
            if expected_count < 0:
                raise RuntimeError("thumbnail_expected_count_invalid")
            valid_artifacts = []
            offsets: set[int] = set()
            for artifact in generation.artifacts:
                if artifact.offset_seconds in offsets:
                    continue
                try:
                    source = ThumbnailArtifactService.validate_artifact(workspace, artifact)
                except ValueError as exc:
                    logger.warning(
                        "Invalid thumbnail artifact media_id={} path={} detail={}",
                        media.id,
                        artifact.relative_path,
                        exc,
                    )
                    continue
                offsets.add(artifact.offset_seconds)
                valid_artifacts.append((artifact, source))
            minimum_count = cls.minimum_acceptable_count(expected_count)
            if len(valid_artifacts) < minimum_count:
                raise RuntimeError(
                    "thumbnail_generation_insufficient_count "
                    f"expected={expected_count} minimum={minimum_count} "
                    f"actual={len(valid_artifacts)}"
                )
            return ThumbnailArtifactService.persist(media, valid_artifacts)

    @staticmethod
    def _error_code(exc: Exception) -> str:
        error_code = getattr(exc, "error_code", None) or getattr(exc, "code", None)
        if isinstance(error_code, str) and error_code.strip():
            return error_code.strip()[:64]
        detail = str(exc).strip()
        if detail:
            return detail.split(maxsplit=1)[0].split(":", maxsplit=1)[0][:64]
        return type(exc).__name__.lower()[:64]

    @staticmethod
    def _error_detail(exc: Exception) -> str:
        return (str(exc).strip() or type(exc).__name__)[:4000]

    @classmethod
    def _write_state(
        cls,
        media: Media,
        *,
        state: str,
        attempt_count: int,
        deferred_count: int,
        next_retry_at,
        error_code: str | None,
        error_detail: str | None,
        terminal_at,
    ) -> None:
        Media.update(
            thumbnail_generation_state=state,
            thumbnail_attempt_count=attempt_count,
            thumbnail_deferred_count=deferred_count,
            thumbnail_next_retry_at=next_retry_at,
            thumbnail_last_error_code=error_code,
            thumbnail_last_error=error_detail,
            thumbnail_terminal_at=terminal_at,
            updated_at=utc_now_for_db(),
        ).where(Media.id == media.id).execute()

    @classmethod
    def _mark_succeeded(cls, media: Media) -> None:
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_SUCCEEDED,
            attempt_count=0,
            deferred_count=0,
            next_retry_at=None,
            error_code=None,
            error_detail=None,
            terminal_at=None,
        )

    @classmethod
    def _mark_deferred(cls, media: Media, exc: ThumbnailDeferred) -> bool:
        now = utc_now_for_db()
        attempt_count = int(media.thumbnail_attempt_count or 0)
        deferred_count = int(media.thumbnail_deferred_count or 0) + 1
        if deferred_count > exc.max_deferred_attempts:
            cls._write_state(
                media,
                state=Media.THUMBNAIL_STATE_TERMINAL,
                attempt_count=attempt_count,
                deferred_count=deferred_count,
                next_retry_at=None,
                error_code=cls._error_code(exc),
                error_detail=cls._error_detail(exc),
                terminal_at=now,
            )
            return True
        backoff_seconds = min(
            exc.deferred_backoff_base_seconds * deferred_count,
            cls.FAILURE_RETRY_BACKOFF_MAX_SECONDS,
        )
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_RETRY_WAIT,
            attempt_count=attempt_count,
            deferred_count=deferred_count,
            next_retry_at=now + timedelta(seconds=backoff_seconds),
            error_code=cls._error_code(exc),
            error_detail=cls._error_detail(exc),
            terminal_at=None,
        )
        return False

    @classmethod
    def _mark_failure(cls, media: Media, exc: Exception) -> bool:
        now = utc_now_for_db()
        attempt_count = int(media.thumbnail_attempt_count or 0) + 1
        error_code = cls._error_code(exc)
        retryable = getattr(exc, "retryable", True)
        is_terminal = (
            not retryable
            or error_code in cls.TERMINAL_ERROR_CODES
            or attempt_count >= cls.MAX_FAILURE_ATTEMPTS
        )
        if is_terminal:
            cls._write_state(
                media,
                state=Media.THUMBNAIL_STATE_TERMINAL,
                attempt_count=attempt_count,
                deferred_count=int(media.thumbnail_deferred_count or 0),
                next_retry_at=None,
                error_code=error_code,
                error_detail=cls._error_detail(exc),
                terminal_at=now,
            )
            return True
        backoff_seconds = min(
            cls.FAILURE_RETRY_BACKOFF_BASE_SECONDS * attempt_count,
            cls.FAILURE_RETRY_BACKOFF_MAX_SECONDS,
        )
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_RETRY_WAIT,
            attempt_count=attempt_count,
            deferred_count=int(media.thumbnail_deferred_count or 0),
            next_retry_at=now + timedelta(seconds=backoff_seconds),
            error_code=error_code,
            error_detail=cls._error_detail(exc),
            terminal_at=None,
        )
        return False

    @classmethod
    def _generate_one(cls, media_id: int) -> ThumbnailGenerationOutcome:
        ensure_database_ready()
        media = Media.get_or_none(Media.id == media_id)
        if media is None or not media.valid:
            return ThumbnailGenerationOutcome("skipped")
        if MediaThumbnail.select().where(MediaThumbnail.media == media).exists():
            cls._mark_succeeded(media)
            return ThumbnailGenerationOutcome("skipped")
        try:
            generated_count = cls._generate_artifacts(media)
        except ThumbnailBackendUnavailable as exc:
            logger.warning(
                "Media thumbnail backend unavailable media_id={} code={} detail={}",
                media_id,
                exc.error_code,
                exc,
            )
            return ThumbnailGenerationOutcome("backend_unavailable", error_code=exc.error_code)
        except ProviderUnavailableError:
            deferred = ThumbnailDeferred(
                "媒体提供方暂不可用",
                error_code="provider_not_installed",
                max_deferred_attempts=cls.MAX_DEFERRED_ATTEMPTS,
                deferred_backoff_base_seconds=cls.DEFERRED_BACKOFF_BASE_SECONDS,
            )
            terminal = cls._mark_deferred(media, deferred)
            return ThumbnailGenerationOutcome(
                "terminal_failed" if terminal else "deferred",
                error_code=deferred.error_code,
            )
        except ThumbnailDeferred as exc:
            terminal = cls._mark_deferred(media, exc)
            return ThumbnailGenerationOutcome(
                "terminal_failed" if terminal else "deferred",
                error_code=cls._error_code(exc),
            )
        except Exception as exc:
            if MediaThumbnail.select().where(MediaThumbnail.media == media).exists():
                cls._mark_succeeded(media)
                return ThumbnailGenerationOutcome("succeeded")
            terminal = cls._mark_failure(media, exc)
            logger.warning(
                "Media thumbnail generation failed media_id={} code={} terminal={} detail={}",
                media_id,
                cls._error_code(exc),
                terminal,
                exc,
            )
            return ThumbnailGenerationOutcome(
                "terminal_failed" if terminal else "retryable_failed",
                error_code=cls._error_code(exc),
            )
        cls._mark_succeeded(media)
        return ThumbnailGenerationOutcome("succeeded", generated_count=generated_count)

    @classmethod
    def generate_pending_thumbnails(cls, *, reporter) -> dict[str, Any]:
        started_at = time.time()
        entries = cls._candidate_entries()
        stats: dict[str, Any] = {
            "pending_media": len(entries),
            "successful_media": 0,
            "generated_thumbnails": 0,
            "deferred_media": 0,
            "retryable_failed_media": 0,
            "terminal_failed_media": 0,
            "failed_media_ids": [],
            "terminal_failed_media_ids": [],
            "backend_failed_lanes": 0,
            "backend_deferred_media": 0,
            "backend_failure_codes": [],
            "skipped_media": 0,
        }
        paused_lanes: set[tuple[str, int]] = set()
        for completed, (media_id, lane) in enumerate(entries, start=1):
            if lane in paused_lanes:
                stats["backend_deferred_media"] += 1
                reporter.emit(current=completed, total=len(entries), summary_patch=stats)
                continue
            outcome = cls._generate_one(media_id)
            if outcome.state == "backend_unavailable":
                paused_lanes.add(lane)
                stats["backend_failed_lanes"] += 1
                stats["backend_deferred_media"] += 1
                if outcome.error_code:
                    stats["backend_failure_codes"].append(outcome.error_code)
            elif outcome.state == "succeeded":
                stats["successful_media"] += 1
                stats["generated_thumbnails"] += outcome.generated_count
            elif outcome.state == "deferred":
                stats["deferred_media"] += 1
            elif outcome.state == "retryable_failed":
                stats["retryable_failed_media"] += 1
                stats["failed_media_ids"].append(media_id)
            elif outcome.state == "terminal_failed":
                stats["terminal_failed_media"] += 1
                stats["failed_media_ids"].append(media_id)
                stats["terminal_failed_media_ids"].append(media_id)
            else:
                stats["skipped_media"] += 1
            reporter.emit(current=completed, total=len(entries), summary_patch=stats)
        logger.info(
            "Finished media thumbnail generation pending_media={} successful_media={} "
            "generated_thumbnails={} terminal_failed_media={} elapsed_ms={}",
            stats["pending_media"],
            stats["successful_media"],
            stats["generated_thumbnails"],
            stats["terminal_failed_media"],
            int((time.time() - started_at) * 1000),
        )
        return stats


__all__ = ["MediaThumbnailTaskService", "ThumbnailGenerationOutcome"]
