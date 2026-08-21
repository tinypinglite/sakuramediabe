"""影片订阅资源查询的领域状态与重试预算。"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import Movie

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED_RETRYABLE = "failed_retryable"
STATE_EXHAUSTED = "exhausted"

ERROR_CODE_NO_CANDIDATE = "no_candidate_found"
INTERRUPTED_ERROR_MESSAGE = "订阅影片资源查询任务中断，等待重试"


class SubscriptionSearchError(RuntimeError):
    def __init__(self, code: str, message: str, *, consumes_budget: bool):
        super().__init__(message)
        self.code = code
        self.consumes_budget = consumes_budget


class MovieSubscriptionSearchStateService:
    """只保存订阅搜索真正需要的状态，不再借用通用任务台账。"""

    @staticmethod
    def stale_attempt_limit() -> int:
        return settings.downloads.subscription_search_stale_attempt_limit

    @staticmethod
    def is_fresh(movie: Movie, *, now: datetime) -> bool:
        if movie.release_date is None:
            return False
        return movie.release_date > now - timedelta(
            days=settings.downloads.subscription_search_fresh_days
        )

    @classmethod
    def candidate_condition(cls, *, now: datetime):
        state = Movie.subscription_search_state
        return (
            state.not_in((STATE_RUNNING, STATE_EXHAUSTED))
            & (
                (state != STATE_FAILED_RETRYABLE)
                | Movie.subscription_search_next_retry_at.is_null(True)
                | (Movie.subscription_search_next_retry_at <= now)
            )
        )

    @staticmethod
    def begin_attempt(movie_id: int) -> Movie | None:
        now = utc_now_for_db()
        updated = (
            Movie.update(
                subscription_search_state=STATE_RUNNING,
                subscription_search_last_attempted_at=now,
                subscription_search_next_retry_at=None,
            )
            .where(Movie.id == movie_id)
            .execute()
        )
        return Movie.get_or_none(Movie.id == movie_id) if updated else None

    @staticmethod
    def mark_succeeded(movie_id: int) -> None:
        Movie.update(
            subscription_search_state=STATE_SUCCEEDED,
            subscription_search_attempt_count=0,
            subscription_search_next_retry_at=None,
            subscription_search_error_code=None,
            subscription_search_last_error=None,
            subscription_search_last_error_at=None,
            subscription_search_last_succeeded_at=utc_now_for_db(),
        ).where(Movie.id == movie_id).execute()

    @classmethod
    def mark_failed(
        cls,
        movie: Movie,
        error: SubscriptionSearchError,
    ) -> str:
        now = utc_now_for_db()
        attempt_count = movie.subscription_search_attempt_count
        state = STATE_FAILED_RETRYABLE
        if error.consumes_budget and not cls.is_fresh(movie, now=now):
            attempt_count += 1
            if attempt_count >= cls.stale_attempt_limit():
                state = STATE_EXHAUSTED
        Movie.update(
            subscription_search_state=state,
            subscription_search_attempt_count=attempt_count,
            subscription_search_next_retry_at=None,
            subscription_search_error_code=error.code,
            subscription_search_last_error=str(error),
            subscription_search_last_error_at=now,
        ).where(Movie.id == movie.id).execute()
        return state

    @staticmethod
    def reset(movie_ids: list[int] | None = None) -> int:
        query = Movie.update(
            subscription_search_state=STATE_PENDING,
            subscription_search_attempt_count=0,
            subscription_search_retry_round=Movie.subscription_search_retry_round + 1,
            subscription_search_last_attempted_at=None,
            subscription_search_last_succeeded_at=None,
            subscription_search_next_retry_at=None,
            subscription_search_error_code=None,
            subscription_search_last_error=None,
            subscription_search_last_error_at=None,
        ).where(Movie.is_subscribed == True)
        if movie_ids:
            query = query.where(Movie.id.in_(list(dict.fromkeys(movie_ids))))
        else:
            query = query.where(Movie.subscription_search_state == STATE_EXHAUSTED)
        return query.execute()

    @staticmethod
    def recover_interrupted_running_movies() -> int:
        now = utc_now_for_db()
        return (
            Movie.update(
                subscription_search_state=STATE_FAILED_RETRYABLE,
                subscription_search_next_retry_at=None,
                subscription_search_error_code="task_interrupted",
                subscription_search_last_error=INTERRUPTED_ERROR_MESSAGE,
                subscription_search_last_error_at=now,
            )
            .where(Movie.subscription_search_state == STATE_RUNNING)
            .execute()
        )
