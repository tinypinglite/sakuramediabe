from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import with_movie_card_relations
from sakuramedia_metadata_providers.providers.javdb import JavdbProvider
from sakuramedia_metadata_providers.providers.missav import MissavRankingProvider
from src.model import Media, Movie, RankingItem, get_database
from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.pagination import PageResponse
from src.schema.discovery import RankedMovieListItemResource, RankingBoardResource, RankingSourceResource
from src.service.catalog.catalog_import_service import CatalogImportService


@dataclass(frozen=True)
class RankingBoardDefinition:
    key: str
    name: str
    provider_raw_key: str
    supported_periods: tuple[str, ...]
    default_period: str | None = None


@dataclass(frozen=True)
class RankingSourceDefinition:
    key: str
    name: str
    boards: tuple[RankingBoardDefinition, ...]

    def board_by_key(self, board_key: str) -> RankingBoardDefinition | None:
        for board in self.boards:
            if board.key == board_key:
                return board
        return None


JAVDB_SOURCE = RankingSourceDefinition(
    key="javdb",
    name="JavDB",
    boards=(
        RankingBoardDefinition(
            key="censored",
            name="有码",
            provider_raw_key="0",
            supported_periods=("daily", "weekly", "monthly"),
            default_period="daily",
        ),
        RankingBoardDefinition(
            key="uncensored",
            name="无码",
            provider_raw_key="1",
            supported_periods=("daily", "weekly", "monthly"),
            default_period="daily",
        ),
        RankingBoardDefinition(
            key="fc2",
            name="FC2",
            provider_raw_key="3",
            supported_periods=("daily", "weekly", "monthly"),
            default_period="daily",
        ),
    ),
)

MISSAV_SOURCE = RankingSourceDefinition(
    key="missav",
    name="MissAV",
    boards=(
        RankingBoardDefinition(
            key="all",
            name="综合",
            provider_raw_key="all",
            supported_periods=("daily", "weekly", "monthly"),
            default_period="daily",
        ),
    ),
)

RANKING_SOURCES: dict[str, RankingSourceDefinition] = {
    JAVDB_SOURCE.key: JAVDB_SOURCE,
    MISSAV_SOURCE.key: MISSAV_SOURCE,
}


class RankingCatalogService:
    @staticmethod
    def _require_source(source_key: str) -> RankingSourceDefinition:
        source = RANKING_SOURCES.get(source_key)
        if source is None:
            raise ApiError(
                404,
                "ranking_source_not_found",
                "排行榜来源不存在",
                {"source_key": source_key},
            )
        return source

    @classmethod
    def _require_board(cls, source_key: str, board_key: str) -> RankingBoardDefinition:
        source = cls._require_source(source_key)
        board = source.board_by_key(board_key)
        if board is None:
            raise ApiError(
                404,
                "ranking_board_not_found",
                "排行榜不存在",
                {"source_key": source_key, "board_key": board_key},
            )
        return board

    @staticmethod
    def _resolve_period(board: RankingBoardDefinition, period: str | None) -> str:
        normalized_period = (period or "").strip().lower()
        if board.supported_periods:
            if not normalized_period:
                raise ApiError(
                    422,
                    "invalid_ranking_period",
                    "period is required for this board",
                    {"period": period},
                )
            if normalized_period not in board.supported_periods:
                raise ApiError(
                    422,
                    "invalid_ranking_period",
                    "period is not supported",
                    {
                        "period": period,
                        "supported_periods": list(board.supported_periods),
                    },
                )
            return normalized_period
        if normalized_period:
            raise ApiError(
                422,
                "invalid_ranking_period",
                "period is not supported for this board",
                {"period": period},
            )
        return ""

    @staticmethod
    def list_sources() -> list[RankingSourceResource]:
        return [
            RankingSourceResource(source_key=source.key, name=source.name)
            for source in RANKING_SOURCES.values()
        ]

    @classmethod
    def list_boards(cls, source_key: str) -> list[RankingBoardResource]:
        source = cls._require_source(source_key)
        return [
            RankingBoardResource(
                source_key=source.key,
                board_key=board.key,
                name=board.name,
                supported_periods=list(board.supported_periods),
                default_period=board.default_period,
            )
            for board in source.boards
        ]

    @classmethod
    def list_board_items(
        cls,
        source_key: str,
        board_key: str,
        period: str | None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[RankedMovieListItemResource]:
        board = cls._require_board(source_key, board_key)
        normalized_period = cls._resolve_period(board, period)
        safe_page = max(int(page), 1)
        safe_page_size = max(int(page_size), 1)
        start = (safe_page - 1) * safe_page_size

        base_query = (
            RankingItem.select()
            .where(
                RankingItem.source_key == source_key,
                RankingItem.board_key == board_key,
                RankingItem.period == normalized_period,
            )
            .order_by(RankingItem.rank.asc())
        )
        total = base_query.count()
        ranking_rows = list(base_query.offset(start).limit(safe_page_size))
        if not ranking_rows:
            return PageResponse[RankedMovieListItemResource](
                items=[],
                page=safe_page,
                page_size=safe_page_size,
                total=total,
            )

        movie_ids = [item.movie_id for item in ranking_rows]
        movie_numbers = [item.movie_number for item in ranking_rows]
        movie_query, _thin_cover_alias = with_movie_card_relations(Movie.select(Movie))
        movies = {
            movie.id: movie
            for movie in movie_query.where(Movie.id.in_(movie_ids))
        }
        playable_movie_numbers = {
            movie_number
            for (movie_number,) in Media.select(Media.movie).where(
                Media.valid == True,
                Media.movie.in_(movie_numbers),
            ).tuples()
        }

        items: list[RankedMovieListItemResource] = []
        for ranking_row in ranking_rows:
            movie = movies.get(ranking_row.movie_id)
            if movie is None:
                continue
            movie_item = MovieListItemResource.from_attributes_model(movie)
            movie_item.can_play = movie.movie_number in playable_movie_numbers
            items.append(
                RankedMovieListItemResource.model_validate(
                    {
                        **movie_item.model_dump(),
                        "rank": ranking_row.rank,
                    }
                )
            )
        return PageResponse[RankedMovieListItemResource](
            items=items,
            page=safe_page,
            page_size=safe_page_size,
            total=total,
        )


class RankingSyncService:
    def __init__(
        self,
        import_service: CatalogImportService | None = None,
        providers: dict[str, Any] | None = None,
    ) -> None:
        self.import_service = import_service or CatalogImportService()
        self.providers = providers or {}

    @staticmethod
    def _build_javdb_provider() -> JavdbProvider:
        from src.metadata.factory import build_javdb_provider
        return build_javdb_provider()

    @staticmethod
    def _build_missav_ranking_provider() -> MissavRankingProvider:
        from src.metadata.factory import build_missav_ranking_provider

        return build_missav_ranking_provider()

    def _provider_for_source(self, source_key: str) -> Any:
        provider = self.providers.get(source_key)
        if provider is not None:
            return provider
        if source_key == "javdb":
            provider = self._build_javdb_provider()
            self.providers[source_key] = provider
            return provider
        if source_key == "missav":
            provider = self._build_missav_ranking_provider()
            self.providers[source_key] = provider
            return provider
        raise ValueError(f"unsupported ranking source: {source_key}")

    def _get_rank_numbers(
        self,
        *,
        source_key: str,
        board: RankingBoardDefinition,
        period: str,
    ) -> list[str]:
        if source_key == "javdb":
            provider = self._provider_for_source("javdb")
            return provider.get_rank_numbers(
                video_type=board.provider_raw_key,
                period=period,
            )
        if source_key == "missav":
            provider = self._provider_for_source("missav")
            return provider.fetch_rank_numbers(period)
        raise ValueError(f"unsupported ranking source: {source_key}")

    def _get_movie_detail(self, source_key: str, movie_number: str) -> Any:
        # MissAV 只提供榜单番号，影片详情统一继续走 JavDB。
        if source_key in {"javdb", "missav"}:
            return self._provider_for_source("javdb").get_movie_by_number(movie_number)
        raise ValueError(f"unsupported ranking source: {source_key}")

    def _replace_scope_items(
        self,
        source_key: str,
        board_key: str,
        period: str,
        rows: list[dict[str, Any]],
    ) -> int:
        with get_database().atomic():
            RankingItem.delete().where(
                RankingItem.source_key == source_key,
                RankingItem.board_key == board_key,
                RankingItem.period == period,
            ).execute()
            if not rows:
                return 0
            RankingItem.insert_many(rows).execute()
            return len(rows)

    def sync_board_period(
        self,
        source_key: str,
        board_key: str,
        period: str | None,
    ) -> dict[str, int | str]:
        board = RankingCatalogService._require_board(source_key, board_key)
        normalized_period = RankingCatalogService._resolve_period(board, period)
        now = utc_now_for_db()
        movie_numbers = self._get_rank_numbers(
            source_key=source_key,
            board=board,
            period=normalized_period,
        )

        imported_count = 0
        skipped_count = 0
        insert_rows: list[dict[str, Any]] = []
        for rank, movie_number in enumerate(movie_numbers, start=1):
            try:
                detail = self._get_movie_detail(source_key, movie_number)
                movie = self.import_service.upsert_movie_from_javdb_detail(detail)
            except Exception as exc:
                skipped_count += 1
                logger.warning(
                    "Ranking sync item skipped source_key={} board_key={} period={} rank={} movie_number={} detail={}",
                    source_key,
                    board_key,
                    normalized_period,
                    rank,
                    movie_number,
                    exc,
                )
                continue

            imported_count += 1
            insert_rows.append(
                {
                    "source_key": source_key,
                    "board_key": board_key,
                    "period": normalized_period,
                    "rank": rank,
                    "movie_number": movie.movie_number,
                    "movie": movie.id,
                    "created_at": now,
                    "updated_at": now,
                }
            )

        stored_count = self._replace_scope_items(
            source_key=source_key,
            board_key=board_key,
            period=normalized_period,
            rows=insert_rows,
        )
        return {
            "source_key": source_key,
            "board_key": board_key,
            "period": normalized_period,
            "fetched_numbers": len(movie_numbers),
            "imported_movies": imported_count,
            "skipped_movies": skipped_count,
            "stored_items": stored_count,
        }

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    def sync_all_rankings(self, progress_callback=None) -> dict[str, int]:
        stats = {
            "total_targets": 0,
            "success_targets": 0,
            "failed_targets": 0,
            "fetched_numbers": 0,
            "imported_movies": 0,
            "skipped_movies": 0,
            "stored_items": 0,
        }
        total_targets = sum(
            len(board.supported_periods or ("",))
            for source in RANKING_SOURCES.values()
            for board in source.boards
        )
        completed_targets = 0
        self._emit_progress(
            progress_callback,
            current=0,
            total=total_targets,
            text="开始同步排行榜",
            summary_patch=stats,
        )
        for source in RANKING_SOURCES.values():
            for board in source.boards:
                periods = board.supported_periods or ("",)
                for period in periods:
                    stats["total_targets"] += 1
                    try:
                        target_stats = self.sync_board_period(
                            source_key=source.key,
                            board_key=board.key,
                            period=period,
                        )
                    except Exception as exc:
                        stats["failed_targets"] += 1
                        logger.warning(
                            "Ranking sync target failed source_key={} board_key={} period={} detail={}",
                            source.key,
                            board.key,
                            period,
                            exc,
                        )
                        completed_targets += 1
                        self._emit_progress(
                            progress_callback,
                            current=completed_targets,
                            total=total_targets,
                            text=f"排行榜同步失败 {source.name}-{board.name}-{period}",
                            summary_patch=stats,
                        )
                        continue
                    stats["success_targets"] += 1
                    stats["fetched_numbers"] += int(target_stats["fetched_numbers"])
                    stats["imported_movies"] += int(target_stats["imported_movies"])
                    stats["skipped_movies"] += int(target_stats["skipped_movies"])
                    stats["stored_items"] += int(target_stats["stored_items"])
                    completed_targets += 1
                    self._emit_progress(
                        progress_callback,
                        current=completed_targets,
                        total=total_targets,
                        text=f"已同步 {source.name}-{board.name}-{period}",
                        summary_patch=stats,
                    )
        return stats
