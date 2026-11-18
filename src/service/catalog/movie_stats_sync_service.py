"""按绑定插件刷新影片互动数与热度。"""

from __future__ import annotations

from datetime import timedelta

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.i18n import translate
from src.model import Movie
from src.plugins.extensions.stats import (
    PluginMovieStatsSnapshot,
    validate_stats_snapshot,
)
from src.service.catalog.movie_stats_service import MovieStatsService
from src.service.catalog.movie_stats_source_service import (
    MovieStatsSourceService,
    MovieStatsSourceUnavailable,
)


class MovieStatsSyncService:
    TASK_KEY = "movie_stats_sync"
    RECENT_REFRESH_INTERVAL = timedelta(days=2)
    MIDDLE_REFRESH_INTERVAL = timedelta(days=7)
    OLD_REFRESH_INTERVAL = timedelta(days=30)

    @classmethod
    def _candidate_query(cls, plugin_id: str, heat_version: str):
        now = utc_now_for_db()
        recent_since = now - timedelta(days=60)
        middle_since = now - timedelta(days=180)
        stale_or_missing = Movie.stats_updated_at.is_null(True)
        source_mismatch = Movie.stats_source.is_null(True) | (
            Movie.stats_source != plugin_id
        )
        version_mismatch = Movie.heat_version.is_null(True) | (
            Movie.heat_version != heat_version
        )
        subscribed_due = (
            (Movie.is_subscribed == True)
            & Movie.subscribed_at.is_null(False)
            & (
                stale_or_missing
                | (Movie.subscribed_at > Movie.stats_updated_at)
            )
        )
        recent_due = (Movie.release_date >= recent_since) & (
            stale_or_missing
            | (Movie.stats_updated_at <= now - cls.RECENT_REFRESH_INTERVAL)
        )
        middle_due = (
            (Movie.release_date >= middle_since)
            & (Movie.release_date < recent_since)
            & (
                stale_or_missing
                | (Movie.stats_updated_at <= now - cls.MIDDLE_REFRESH_INTERVAL)
            )
        )
        old_due = (
            (Movie.release_date.is_null(True) | (Movie.release_date < middle_since))
            & (
                stale_or_missing
                | (Movie.stats_updated_at <= now - cls.OLD_REFRESH_INTERVAL)
            )
        )
        return Movie.select(Movie.id, Movie.movie_number).where(
            source_mismatch
            | version_mismatch
            | subscribed_due
            | recent_due
            | middle_due
            | old_due
        ).order_by(Movie.id)

    @classmethod
    def run(cls, *, reporter, params=None) -> dict:
        del params
        try:
            plugin_id, source = MovieStatsSourceService.bound_source()
        except MovieStatsSourceUnavailable as exc:
            logger.info("Movie stats sync skipped: {}", exc)
            return {
                "status": "skipped",
                "reason": "stats_source_unavailable",
                "detail": str(exc),
                "candidate_movies": 0,
                "processed_movies": 0,
                "succeeded_movies": 0,
                "failed_movies": 0,
                "updated_movies": 0,
                "unchanged_movies": 0,
                "no_data_movies": 0,
                "failed_movie_ids": [],
            }

        candidates = list(cls._candidate_query(plugin_id, source.heat_version))
        total = len(candidates)
        stats: dict = {
            "status": "ok",
            "stats_source": plugin_id,
            "candidate_movies": total,
            "processed_movies": 0,
            "succeeded_movies": 0,
            "failed_movies": 0,
            "updated_movies": 0,
            "unchanged_movies": 0,
            "no_data_movies": 0,
            "failed_movie_ids": [],
        }
        logger.info(
            "Movie stats sync started plugin={} candidate_movies={}",
            plugin_id,
            total,
        )
        step = max(total // 20, 1)

        def progress_text(completed: int, *, movie_number: str | None = None) -> str:
            params = {
                "current": completed,
                "total": total,
                "succeeded": stats["succeeded_movies"],
                "failed": stats["failed_movies"],
                "no_data": stats["no_data_movies"],
            }
            if movie_number:
                return translate(
                    "tasks.movie_stats_sync.progress.refreshing",
                    movie_number=movie_number,
                    **params,
                )
            return translate("tasks.movie_stats_sync.progress.running", **params)

        for current, movie in enumerate(candidates, start=1):
            reporter.emit(
                current=current - 1,
                total=total,
                text=progress_text(current - 1, movie_number=movie.movie_number),
                summary_patch=stats,
            )
            try:
                result = source.fetch_stats(movie.movie_number)
                if result is None:
                    stats["no_data_movies"] += 1
                else:
                    snapshot = PluginMovieStatsSnapshot.model_validate(result)
                    validate_stats_snapshot(source, snapshot)
                    changed = MovieStatsService.apply_snapshot(
                        movie.id,
                        plugin_id=plugin_id,
                        snapshot=snapshot,
                        heat_version=source.heat_version,
                    )
                    stats["succeeded_movies"] += 1
                    stats["updated_movies" if changed else "unchanged_movies"] += 1
            except Exception:
                logger.exception(
                    "Movie stats sync failed plugin={} movie_id={} movie_number={}",
                    plugin_id,
                    movie.id,
                    movie.movie_number,
                )
                stats["failed_movies"] += 1
                stats["failed_movie_ids"].append(movie.id)
            finally:
                stats["processed_movies"] += 1
                reporter.emit(
                    current=current,
                    total=total,
                    text=progress_text(current),
                    summary_patch=stats,
                )
                if current == 1 or current % step == 0:
                    logger.info(
                        "Movie stats sync progress completed={}/{} succeeded={} failed={} no_data={}",
                        current,
                        total,
                        stats["succeeded_movies"],
                        stats["failed_movies"],
                        stats["no_data_movies"],
                    )
        return stats
