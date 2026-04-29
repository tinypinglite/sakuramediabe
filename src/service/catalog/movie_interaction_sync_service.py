from __future__ import annotations

from datetime import date, datetime, time, timedelta

from loguru import logger

from sakuramedia_metadata_providers.providers.javdb import JavdbProvider
from src.model import Movie, RankingItem, ResourceTaskState, get_database
from sakuramedia_metadata_providers.models import JavdbMovieDetailResource
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.system.resource_task_state_service import ResourceTaskStateService


class MovieInteractionSyncService:
    TASK_KEY = "movie_interaction_sync"
    SYNC_STATUS_PENDING = ResourceTaskStateService.STATE_PENDING
    SYNC_STATUS_RUNNING = ResourceTaskStateService.STATE_RUNNING
    SYNC_STATUS_SUCCEEDED = ResourceTaskStateService.STATE_SUCCEEDED
    SYNC_STATUS_FAILED = ResourceTaskStateService.STATE_FAILED
    INTERRUPTED_SYNC_ERROR_MESSAGE = "影片互动数同步任务中断，等待重试"
    RANKING_REFRESH_INTERVAL = timedelta(hours=1)
    SUBSCRIBED_REFRESH_INTERVAL = timedelta(days=1)
    RECENT_REFRESH_INTERVAL = timedelta(days=1)
    MIDDLE_REFRESH_INTERVAL = timedelta(days=3)
    DEFAULT_REFRESH_INTERVAL = timedelta(days=7)

    def __init__(self, provider: JavdbProvider | None = None):
        self.provider = provider or self._build_javdb_provider()

    @staticmethod
    def _build_javdb_provider() -> JavdbProvider:
        from src.metadata.factory import build_javdb_provider

        return build_javdb_provider()

    @staticmethod
    def _now() -> datetime:
        from src.common.runtime_time import utc_now_for_db

        return utc_now_for_db()

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    @classmethod
    def recover_interrupted_running_movies(cls, *, error_message: str | None = None) -> int:
        normalized_error = (error_message or "").strip() or cls.INTERRUPTED_SYNC_ERROR_MESSAGE
        # 仅回收卡在 running 的影片，避免脏状态长期阻塞后续同步。
        return ResourceTaskStateService.recover_running_records(cls.TASK_KEY, normalized_error)

    @classmethod
    def _normalize_release_date(cls, value: datetime | date | str | None) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, time.min)
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return None
            try:
                return datetime.fromisoformat(normalized)
            except ValueError:
                logger.warning("Movie interaction sync skipped invalid release_date value={}", normalized)
                return None
        return None

    @classmethod
    def _resolve_refresh_interval(
        cls,
        movie: Movie,
        *,
        now: datetime,
        ranked_movie_ids: set[int],
    ) -> timedelta:
        # 排行榜影片优先按小时刷新，保证榜单上的互动数据更快回刷。
        if movie.id in ranked_movie_ids:
            return cls.RANKING_REFRESH_INTERVAL
        if bool(movie.is_subscribed):
            return cls.SUBSCRIBED_REFRESH_INTERVAL

        release_date = cls._normalize_release_date(movie.release_date)
        if release_date is None:
            return cls.DEFAULT_REFRESH_INTERVAL

        # 发布时间分层按当前时点滚动判断，未来日期默认归入最近 60 天档。
        if release_date >= now - timedelta(days=60):
            return cls.RECENT_REFRESH_INTERVAL
        if release_date >= now - timedelta(days=180):
            return cls.MIDDLE_REFRESH_INTERVAL
        return cls.DEFAULT_REFRESH_INTERVAL

    @classmethod
    def _load_last_succeeded_at_by_movie_ids(cls, movie_ids: list[int]) -> dict[int, datetime]:
        if not movie_ids:
            return {}
        query = (
            ResourceTaskState.select(
                ResourceTaskState.resource_id,
                ResourceTaskState.last_succeeded_at,
            )
            .where(
                ResourceTaskState.task_key == cls.TASK_KEY,
                ResourceTaskState.resource_type == "movie",
                ResourceTaskState.resource_id.in_(movie_ids),
            )
        )
        return {
            int(resource_id): last_succeeded_at
            for resource_id, last_succeeded_at in query.tuples()
            if last_succeeded_at is not None
        }

    @classmethod
    def _load_ranked_movie_ids(cls, movie_ids: list[int]) -> set[int]:
        if not movie_ids:
            return set()
        query = (
            RankingItem.select(RankingItem.movie)
            .where(RankingItem.movie.in_(movie_ids))
            .distinct()
        )
        return {int(movie_id) for (movie_id,) in query.tuples()}

    @classmethod
    def _is_due_for_sync(
        cls,
        movie: Movie,
        *,
        now: datetime,
        last_succeeded_at: datetime | None,
        ranked_movie_ids: set[int],
    ) -> bool:
        if last_succeeded_at is None:
            return True

        refresh_interval = cls._resolve_refresh_interval(
            movie,
            now=now,
            ranked_movie_ids=ranked_movie_ids,
        )
        return last_succeeded_at + refresh_interval <= now

    @classmethod
    def _candidate_query(cls):
        return Movie.select().order_by(Movie.is_subscribed.desc(), Movie.id.asc())

    def _collect_candidates(self, *, now: datetime) -> list[Movie]:
        movies = list(self._candidate_query())
        movie_ids = [movie.id for movie in movies]
        # 小时级调度下批量预加载状态与在榜集合，避免逐片查询放大数据库压力。
        last_succeeded_at_by_movie_id = self._load_last_succeeded_at_by_movie_ids(movie_ids)
        ranked_movie_ids = self._load_ranked_movie_ids(movie_ids)
        candidates: list[Movie] = []
        for movie in movies:
            if self._is_due_for_sync(
                movie,
                now=now,
                last_succeeded_at=last_succeeded_at_by_movie_id.get(movie.id),
                ranked_movie_ids=ranked_movie_ids,
            ):
                candidates.append(movie)
        return candidates

    @classmethod
    def _build_interaction_payload(cls, detail: JavdbMovieDetailResource) -> dict[str, int | float]:
        return {
            "score": detail.score or 0,
            "score_number": detail.score_number,
            "watched_count": detail.watched_count,
            "want_watch_count": detail.want_watch_count,
            "comment_count": detail.comment_count,
        }

    def _mark_movie_started(self, movie: Movie) -> None:
        ResourceTaskStateService.mark_started(self.TASK_KEY, movie.id)

    def _mark_movie_failed(self, movie: Movie, error_message: str) -> None:
        ResourceTaskStateService.mark_failed(self.TASK_KEY, movie.id, error_message)

    def _sync_movie_interactions(self, movie: Movie) -> tuple[bool, bool, int]:
        self._mark_movie_started(movie)
        detail = self.provider.get_movie_by_javdb_id(movie.javdb_id)
        interaction_payload = self._build_interaction_payload(detail)

        updated_fields = [
        ]
        interaction_changed = False
        for field_name, target_value in interaction_payload.items():
            if getattr(movie, field_name) == target_value:
                continue
            interaction_changed = True
            setattr(movie, field_name, target_value)
            updated_fields.append(Movie._meta.fields[field_name])

        heat_updated_count = 0
        database = get_database()
        with database.atomic():
            # 互动数与同步状态统一落库，避免出现“数字已更新但状态仍是 running”。
            if updated_fields:
                movie.save(only=updated_fields)
            ResourceTaskStateService.mark_succeeded(self.TASK_KEY, movie.id)
            if interaction_changed:
                heat_updated_count = MovieHeatService.update_single_movie_heat(movie.id)

        return True, interaction_changed, heat_updated_count

    def sync_movie(self, movie: Movie) -> dict[str, int | str]:
        latest_movie = Movie.get_by_id(movie.id)
        try:
            _, interaction_changed, heat_updated_count = self._sync_movie_interactions(latest_movie)
            return {
                "movie_id": latest_movie.id,
                "movie_number": latest_movie.movie_number,
                "updated_movies": 1 if interaction_changed else 0,
                "unchanged_movies": 0 if interaction_changed else 1,
                "heat_updated_movies": heat_updated_count,
            }
        except Exception as exc:
            self._mark_movie_failed(latest_movie, str(exc))
            logger.warning(
                "Movie interaction sync failed movie_id={} movie_number={} javdb_id={} detail={}",
                latest_movie.id,
                latest_movie.movie_number,
                latest_movie.javdb_id,
                exc,
            )
            raise

    def run(self, *, progress_callback=None) -> dict[str, int]:
        now = self._now()
        candidates = self._collect_candidates(now=now)
        stats = {
            "candidate_movies": len(candidates),
            "processed_movies": 0,
            "succeeded_movies": 0,
            "failed_movies": 0,
            "updated_movies": 0,
            "unchanged_movies": 0,
            "heat_updated_movies": 0,
        }
        self._emit_progress(
            progress_callback,
            current=0,
            total=stats["candidate_movies"],
            text="开始同步影片互动数",
            summary_patch=stats,
        )

        for movie in candidates:
            stats["processed_movies"] += 1
            latest_movie = Movie.get_by_id(movie.id)
            try:
                _, interaction_changed, heat_updated_count = self._sync_movie_interactions(latest_movie)
                stats["succeeded_movies"] += 1
                if interaction_changed:
                    stats["updated_movies"] += 1
                else:
                    stats["unchanged_movies"] += 1
                stats["heat_updated_movies"] += heat_updated_count
                self._emit_progress(
                    progress_callback,
                    current=stats["processed_movies"],
                    total=stats["candidate_movies"],
                    text=f"同步互动数成功 {latest_movie.movie_number}",
                    summary_patch=stats,
                )
            except Exception as exc:
                stats["failed_movies"] += 1
                self._mark_movie_failed(latest_movie, str(exc))
                logger.warning(
                    "Movie interaction sync failed movie_id={} movie_number={} javdb_id={} detail={}",
                    latest_movie.id,
                    latest_movie.movie_number,
                    latest_movie.javdb_id,
                    exc,
                )
                self._emit_progress(
                    progress_callback,
                    current=stats["processed_movies"],
                    total=stats["candidate_movies"],
                    text=f"同步互动数失败 {latest_movie.movie_number}",
                    summary_patch=stats,
                )

        return stats
