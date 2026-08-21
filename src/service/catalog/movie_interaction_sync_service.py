from __future__ import annotations

from datetime import timedelta

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.metadata._providers.exceptions import MetadataRequestError
from src.metadata._providers.javdb import JavdbProvider
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError
from src.model import Movie, get_database
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.catalog.movie_heat_service import MovieHeatService


class MovieInteractionSyncService:
    """按影片领域时间戳刷新互动数，不再维护通用资源任务台账。"""

    TASK_KEY = "movie_interaction_sync"
    INTERACTION_FIELDS = (
        "score",
        "score_number",
        "watched_count",
        "want_watch_count",
        "comment_count",
    )
    RECENT_REFRESH_INTERVAL = timedelta(days=2)
    MIDDLE_REFRESH_INTERVAL = timedelta(days=7)

    def __init__(
        self,
        provider: JavdbProvider | None = None,
        catalog_import_service: CatalogImportService | None = None,
    ):
        self.provider = provider or build_javdb_provider()
        self.catalog_import_service = catalog_import_service or CatalogImportService()

    @classmethod
    def _candidate_ids(cls) -> list[int]:
        query = Movie.select(Movie.id)
        now = utc_now_for_db()
        recent_since = now - timedelta(days=60)
        middle_since = now - timedelta(days=180)
        due = (
            Movie.interaction_synced_at.is_null(True)
            | (
                (Movie.is_subscribed == True)
                & Movie.subscribed_at.is_null(False)
                & (Movie.subscribed_at > Movie.interaction_synced_at)
            )
            | (
                (Movie.release_date >= recent_since)
                & (Movie.interaction_synced_at <= now - cls.RECENT_REFRESH_INTERVAL)
            )
            | (
                (Movie.release_date >= middle_since)
                & (Movie.release_date < recent_since)
                & (Movie.interaction_synced_at <= now - cls.MIDDLE_REFRESH_INTERVAL)
            )
        )
        query = query.where(due)
        return [int(movie_id) for (movie_id,) in query.order_by(Movie.id).tuples()]

    def _fetch_and_apply(self, movie: Movie) -> tuple[bool, int]:
        detail = self.provider.get_movie_by_javdb_id(movie.javdb_id)
        with get_database().atomic():
            updated_movie, _created, updated_fields = (
                self.catalog_import_service.update_movie_fields(
                    detail,
                    self.INTERACTION_FIELDS,
                )
            )
            heat_updated_count = 0
            if updated_fields:
                heat_updated_count = MovieHeatService.update_single_movie_heat(
                    updated_movie.id
                )
            # 即使互动数字未变化也算成功刷新，避免下次调度立即重复请求。
            updated_movie.interaction_synced_at = utc_now_for_db()
            updated_movie.save(only=[Movie.interaction_synced_at])
        return bool(updated_fields), heat_updated_count

    def run(self, *, reporter) -> dict[str, int | list[int]]:
        candidate_ids = self._candidate_ids()
        stats: dict[str, int | list[int]] = {
            "candidate_movies": len(candidate_ids),
            "processed_movies": 0,
            "succeeded_movies": 0,
            "failed_movies": 0,
            "updated_movies": 0,
            "unchanged_movies": 0,
            "heat_updated_movies": 0,
            "failed_movie_ids": [],
        }
        total = len(candidate_ids)
        for current, movie_id in enumerate(candidate_ids, start=1):
            try:
                movie = Movie.get_by_id(movie_id)
                changed, heat_updated = self._fetch_and_apply(movie)
            except MetadataNotFoundError as exc:
                logger.warning(
                    "Movie interaction sync skipped missing JavDB movie movie_id={} detail={}",
                    movie_id,
                    exc,
                )
                stats["failed_movies"] += 1
                stats["failed_movie_ids"].append(movie_id)
            except MetadataRequestError as exc:
                logger.warning(
                    "Movie interaction sync request failed movie_id={} detail={}",
                    movie_id,
                    exc,
                )
                stats["failed_movies"] += 1
                stats["failed_movie_ids"].append(movie_id)
            except Exception:
                logger.exception("Movie interaction sync failed movie_id={}", movie_id)
                stats["failed_movies"] += 1
                stats["failed_movie_ids"].append(movie_id)
            else:
                stats["succeeded_movies"] += 1
                stats["updated_movies" if changed else "unchanged_movies"] += 1
                stats["heat_updated_movies"] += heat_updated
            finally:
                stats["processed_movies"] += 1
                reporter.emit(current=current, total=total)
        return stats
