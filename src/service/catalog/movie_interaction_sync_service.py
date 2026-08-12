from __future__ import annotations

from datetime import date, datetime, time, timedelta

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.metadata._providers.exceptions import MetadataRequestError
from src.metadata.factory import build_javdb_provider
from src.metadata._providers.javdb import JavdbProvider
from src.metadata._providers.models import JavdbMovieDetailResource
from src.metadata.provider import MetadataNotFoundError
from src.model import Movie, ResourceTaskState, get_database
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.system.resource_task_runner import (
    STATE_EXHAUSTED,
    STATE_FAILED_RETRYABLE,
    STATE_FAILED_TERMINAL,
    STATE_RUNNING,
    ResourceTaskLedger,
    ResourceTaskRunner,
    ResourceTaskSpec,
    RetryPolicy,
    TaskItemError,
)


class MovieInteractionSyncService:
    """影片互动数同步（已迁 kernel 记账，任务架构 Wave 2）。

    候选判定是时间分层（succeeded 也会到期重刷），因此不用内核默认状态条件，
    改在 Python 侧组合：分层 due 判定 ∩ 状态排除（失败退避未到期 / 终态 / 耗尽）。
    `last_succeeded_at` 是分层的记忆载体，迁移时必须保留（决策 #11 例外）。
    """

    TASK_KEY = "movie_interaction_sync"
    INTERRUPTED_SYNC_ERROR_MESSAGE = "影片互动数同步任务中断，等待重试"
    # 分层策略：
    #   1. 从未同步过的影片：seed 一次，之后按下面分层决定是否再刷。
    #   2. 最近 60 天新片：2 天。
    #   3. 60~180 天中间档：7 天。
    #   4. 其它（含 180 天前 / 无 release_date）：不再周期刷。
    #   5. 订阅补刷：subscribed_at > last_succeeded_at 触发一次性再同步。
    # 排行榜影片不再因"在榜"被强制刷新，与普通影片走同一套分层。
    RECENT_REFRESH_INTERVAL = timedelta(days=2)
    MIDDLE_REFRESH_INTERVAL = timedelta(days=7)
    # 失败预算：JavDB 请求性失败按小时级退避，5 次/轮后 exhausted；
    # 番号在 JavDB 已消失判 failed_terminal。
    RETRY_POLICY = RetryPolicy(
        max_attempts=5, backoff_base_seconds=3600, backoff_max_seconds=86400
    )

    def __init__(self, provider: JavdbProvider | None = None):
        self.provider = provider or build_javdb_provider()

    @staticmethod
    @classmethod
    def recover_interrupted_running_movies(cls, *, error_message: str | None = None) -> int:
        normalized_error = (error_message or "").strip() or cls.INTERRUPTED_SYNC_ERROR_MESSAGE
        return ResourceTaskLedger.recover_running(cls.TASK_KEY, error_message=normalized_error)

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
    ) -> timedelta | None:
        release_date = cls._normalize_release_date(movie.release_date)
        if release_date is None:
            # 无发布日期无法归档，不落任何周期分层。
            return None

        # 发布时间分层按当前时点滚动判断，未来日期默认归入最近 60 天档。
        if release_date >= now - timedelta(days=60):
            return cls.RECENT_REFRESH_INTERVAL
        if release_date >= now - timedelta(days=180):
            return cls.MIDDLE_REFRESH_INTERVAL
        # 180 天以上的老片不再周期刷，只靠 seed / 订阅补刷 / 手动接口触发。
        return None

    @classmethod
    def _load_state_snapshot_by_movie_ids(
        cls, movie_ids: list[int]
    ) -> dict[int, tuple[str, datetime | None, datetime | None]]:
        if not movie_ids:
            return {}
        query = ResourceTaskState.select(
            ResourceTaskState.resource_id,
            ResourceTaskState.state,
            ResourceTaskState.last_succeeded_at,
            ResourceTaskState.next_retry_at,
        ).where(
            ResourceTaskState.task_key == cls.TASK_KEY,
            ResourceTaskState.resource_type == "movie",
            ResourceTaskState.resource_id.in_(movie_ids),
        )
        return {
            int(resource_id): (state, last_succeeded_at, next_retry_at)
            for resource_id, state, last_succeeded_at, next_retry_at in query.tuples()
        }

    @classmethod
    def _is_due_for_sync(
        cls,
        movie: Movie,
        *,
        now: datetime,
        last_succeeded_at: datetime | None,
    ) -> bool:
        # Seed 首次：任何影片首次都刷一次，之后按下面分层决定是否再刷。
        if last_succeeded_at is None:
            return True

        # 订阅补刷：上次成功之后又发生了订阅动作（首次订阅或重新订阅），补一次。
        subscribed_at = movie.subscribed_at
        if (
            bool(movie.is_subscribed)
            and subscribed_at is not None
            and subscribed_at > last_succeeded_at
        ):
            return True

        refresh_interval = cls._resolve_refresh_interval(
            movie,
            now=now,
        )
        # 不在任何分层、也没有待补订阅：seed 后不再自动周期刷。
        if refresh_interval is None:
            return False
        return last_succeeded_at + refresh_interval <= now

    def _select_candidates(self, _state_condition, only_ids=None) -> list[Movie]:
        now = utc_now_for_db()
        # 候选只取分层筛选所需的列（完整模型由内核分批加载），避免全库全字段驻留内存。
        query = Movie.select(
            Movie.id,
            Movie.release_date,
            Movie.is_subscribed,
            Movie.subscribed_at,
        ).order_by(Movie.id.asc())
        if only_ids:
            query = query.where(Movie.id.in_(list(only_ids)))
        movies = list(query)
        movie_ids = [movie.id for movie in movies]
        # 批量预加载状态，避免逐片查询放大数据库压力。
        snapshot_by_id = self._load_state_snapshot_by_movie_ids(movie_ids)
        candidates: list[Movie] = []
        for movie in movies:
            state, last_succeeded_at, next_retry_at = snapshot_by_id.get(
                movie.id, (None, None, None)
            )
            # 状态排除：失败退避未到期 / 终态 / 耗尽 / 在跑，与内核默认条件语义一致。
            if state in (STATE_RUNNING, STATE_FAILED_TERMINAL, STATE_EXHAUSTED):
                continue
            if state == STATE_FAILED_RETRYABLE:
                if next_retry_at is None or next_retry_at <= now:
                    candidates.append(movie)
                continue
            if only_ids:
                # 手动指定即强制补刷，不再走分层判定。
                candidates.append(movie)
                continue
            if self._is_due_for_sync(
                movie,
                now=now,
                last_succeeded_at=last_succeeded_at,
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

    def _fetch_and_apply(self, movie: Movie) -> tuple[bool, int]:
        """抓详情并落库互动数 + 联动热度；失败抛带 error_code 的 TaskItemError。"""
        try:
            detail = self.provider.get_movie_by_javdb_id(movie.javdb_id)
        except MetadataNotFoundError as exc:
            # 番号在 JavDB 已消失：互动数永远无法再同步，判终态。
            raise TaskItemError("javdb_movie_not_found", str(exc), retryable=False) from exc
        except MetadataRequestError as exc:
            raise TaskItemError("javdb_request_error", str(exc)) from exc

        interaction_payload = self._build_interaction_payload(detail)
        updated_fields = []
        interaction_changed = False
        for field_name, target_value in interaction_payload.items():
            if getattr(movie, field_name) == target_value:
                continue
            interaction_changed = True
            setattr(movie, field_name, target_value)
            updated_fields.append(Movie._meta.fields[field_name])

        heat_updated_count = 0
        with get_database().atomic():
            if updated_fields:
                movie.save(only=updated_fields)
            if interaction_changed:
                heat_updated_count = MovieHeatService.update_single_movie_heat(movie.id)
        return interaction_changed, heat_updated_count

    def _process_one(self, ctx, movie: Movie) -> None:
        latest_movie = Movie.get_by_id(movie.id)
        interaction_changed, heat_updated_count = self._fetch_and_apply(latest_movie)
        counters = ctx.shared
        if interaction_changed:
            counters["updated_movies"] += 1
        else:
            counters["unchanged_movies"] += 1
        counters["heat_updated_movies"] += heat_updated_count

    def run(self, *, reporter, only_ids: list[int] | None = None) -> dict[str, int]:
        spec = ResourceTaskSpec(
            task_key=self.TASK_KEY,
            resource_type="movie",
            retry=self.RETRY_POLICY,
            select_candidates=self._select_candidates,
            process_one=self._process_one,
            setup_run=lambda _ctx: {
                "updated_movies": 0,
                "unchanged_movies": 0,
                "heat_updated_movies": 0,
            },
            # 分层重刷会把 succeeded 行选进候选，领取复核用宽松版。
            claim_eligible=ResourceTaskLedger.resync_claim_eligible,
            resource_model=Movie,
        )
        stats = ResourceTaskRunner.run(spec, reporter, only_ids=only_ids)
        counters = stats.get("shared") or {}
        return {
            "candidate_movies": stats["candidate_count"],
            "processed_movies": stats["candidate_count"] - stats["deferred_count"],
            "succeeded_movies": stats["succeeded_count"],
            "failed_movies": (
                stats["failed_retryable_count"]
                + stats["failed_terminal_count"]
                + stats["exhausted_count"]
            ),
            **counters,
        }
