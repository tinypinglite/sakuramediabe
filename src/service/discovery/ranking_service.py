from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import peewee
from loguru import logger

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    emit_progress,
    with_movie_card_relations,
)
from src.metadata.factory import build_javdb_provider
from src.model import Media, Movie, RankingItem, get_database
from src.schema.catalog.movies import MovieListItemResource
from src.schema.discovery import (
    RankedMovieListItemResource,
    RankingBoardItemsResource,
    RankingBoardResource,
    RankingSourceResource,
)
from src.service.catalog.catalog_import_service import CatalogImportService


@dataclass(frozen=True)
class RankingBoardDefinition:
    key: str
    name: str
    supported_periods: tuple[str, ...] = ()
    default_period: str | None = None
    # 抓取前回调：(period, 该 board+period 是否已有数据) -> 是否抓。
    should_fetch: Callable[[str, bool], bool] | None = None
    # 动态周期提供者（如 top250 年份逐年滚动）；优先于 supported_periods。
    supported_periods_provider: Callable[[], tuple[str, ...]] | None = None
    # 番号抓取回调；由插件提供，宿主只负责编排与写库。
    fetch_numbers: Callable[[str], list[str]] | None = None


@dataclass(frozen=True)
class RankingSourceDefinition:
    key: str
    name: str
    boards: tuple[RankingBoardDefinition, ...]
    # 来源归属插件 ID（内建来源为 None），仅用于 API 展示归属。
    plugin_id: str | None = None

    def board_by_key(self, board_key: str) -> RankingBoardDefinition | None:
        for board in self.boards:
            if board.key == board_key:
                return board
        return None


def board_supported_periods(board: RankingBoardDefinition) -> tuple[str, ...]:
    # 动态周期提供者优先；否则用静态 supported_periods。
    if board.supported_periods_provider is not None:
        return tuple(board.supported_periods_provider())
    return board.supported_periods


# 排行榜来源注册表：宿主不内置任何来源，全部由插件在启动时合并进来。
RANKING_SOURCES: dict[str, RankingSourceDefinition] = {}
# source_key -> plugin_id 归属表：插件只能同步自己声明的来源。
RANKING_SOURCE_OWNERS: dict[str, str] = {}


def register_plugin_ranking_sources(
    sources: list[RankingSourceDefinition],
    owners: dict[str, str],
) -> None:
    """整体重建排行榜注册表与归属表；只由 scheduler adapter 调用。"""
    merged = {source.key: source for source in sources}
    RANKING_SOURCES.clear()
    RANKING_SOURCES.update(merged)
    RANKING_SOURCE_OWNERS.clear()
    RANKING_SOURCE_OWNERS.update(owners)


class RankingCatalogService:
    # 榜单条目允许的排序字段
    RANKING_BOARD_ITEMS_SORT_FIELDS = ("rank", "heat")

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
        supported_periods = board_supported_periods(board)
        if supported_periods:
            if not normalized_period:
                raise ApiError(
                    422,
                    "invalid_ranking_period",
                    "period is required for this board",
                    {"period": period},
                )
            if normalized_period not in supported_periods:
                raise ApiError(
                    422,
                    "invalid_ranking_period",
                    "period is not supported",
                    {
                        "period": period,
                        "supported_periods": list(supported_periods),
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

    @classmethod
    def _build_board_items_sort(
        cls, sort: str | None
    ) -> tuple[list, bool]:
        """解析 ``field:direction`` 排序表达式，返回 (order_by 表达式, 是否需要 JOIN Movie)。"""
        # 未传或为空时保持默认：按榜单原始排名升序
        if sort is None or not sort.strip():
            return [RankingItem.rank.asc()], False

        normalized = sort.strip().lower()
        try:
            field_name, direction = normalized.split(":", 1)
        except ValueError:
            raise ApiError(
                422,
                "invalid_ranking_filter",
                "Invalid sort expression",
                {"sort": sort},
            )

        if (
            field_name not in cls.RANKING_BOARD_ITEMS_SORT_FIELDS
            or direction not in ("asc", "desc")
        ):
            raise ApiError(
                422,
                "invalid_ranking_filter",
                "Invalid sort expression",
                {"sort": sort},
            )

        ascending = direction == "asc"
        tie_breaker = RankingItem.rank.asc() if ascending else RankingItem.rank.desc()
        if field_name == "rank":
            # 按 rank 排序无需 JOIN Movie，且 rank 本身即为唯一键，不再追加 tie-breaker
            return [tie_breaker], False
        # 按 heat 排序需要 JOIN Movie，并补 rank 次级排序避免相同热度翻页错位
        heat_expression = Movie.heat.asc() if ascending else Movie.heat.desc()
        return [heat_expression, tie_breaker], True

    @staticmethod
    def list_sources() -> list[RankingSourceResource]:
        return [
            RankingSourceResource(
                source_key=source.key,
                name=source.name,
                plugin_id=source.plugin_id,
            )
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
                supported_periods=list(board_supported_periods(board)),
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
        sort: str | None = None,
    ) -> RankingBoardItemsResource:
        board = cls._require_board(source_key, board_key)
        normalized_period = cls._resolve_period(board, period)
        safe_page = max(int(page), 1)
        safe_page_size = max(int(page_size), 1)
        start = (safe_page - 1) * safe_page_size

        order_expressions, _needs_movie_join = cls._build_board_items_sort(sort)
        base_query = (
            RankingItem.select()
            .join(Movie, on=(RankingItem.movie == Movie.id))
            .where(
                RankingItem.source_key == source_key,
                RankingItem.board_key == board_key,
                RankingItem.period == normalized_period,
                Movie.is_blacklisted == False,
            )
        )
        base_query = base_query.order_by(*order_expressions)
        total = base_query.count()
        # 该榜单+周期整批的抓取时间（整榜删旧插新，全批一致），与分页无关
        synced_at = (
            RankingItem.select(peewee.fn.MAX(RankingItem.updated_at))
            .where(
                RankingItem.source_key == source_key,
                RankingItem.board_key == board_key,
                RankingItem.period == normalized_period,
            )
            .scalar()
        )
        ranking_rows = list(base_query.offset(start).limit(safe_page_size))
        if not ranking_rows:
            return RankingBoardItemsResource(
                items=[],
                page=safe_page,
                page_size=safe_page_size,
                total=total,
                synced_at=synced_at,
            )

        movie_ids = [item.movie_id for item in ranking_rows]
        movie_numbers = [item.movie_number for item in ranking_rows]
        movie_query, _thin_cover_alias = with_movie_card_relations(Movie.select(Movie))
        movies = {
            movie.id: movie
            for movie in movie_query.where(Movie.id.in_(movie_ids))
        }
        playable_movie_numbers: set[str] = set()
        media_rows = (
            Media.select(Media.movie)
            .where(
                Media.valid == True,
                Media.movie.in_(movie_numbers),
            )
            .tuples()
        )
        for (movie_number,) in media_rows:
            playable_movie_numbers.add(movie_number)

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
        return RankingBoardItemsResource(
            items=items,
            page=safe_page,
            page_size=safe_page_size,
            total=total,
            synced_at=synced_at,
        )


class RankingSyncService:
    def __init__(
        self,
        import_service: CatalogImportService | None = None,
        providers: dict[str, Any] | None = None,
    ) -> None:
        self.import_service = import_service or CatalogImportService()
        self.providers = providers or {}

    def _get_rank_numbers(
        self,
        *,
        board: RankingBoardDefinition,
        period: str,
    ) -> list[str]:
        if board.fetch_numbers is None:
            raise ValueError(f"ranking board 缺少 fetch_numbers: {board.key}")
        numbers = board.fetch_numbers(period)
        if not isinstance(numbers, list) or not all(
            isinstance(number, str) for number in numbers
        ):
            raise ValueError(
                f"fetch_numbers 必须返回 list[str]: board={board.key} "
                f"got={type(numbers)}"
            )
        return numbers

    def _get_movie_detail(self, source_key: str, movie_number: str) -> Any:
        # 排行榜排的都是 JAV 主库影片，详情统一走 JavDB provider。
        provider = self.providers.get(source_key)
        if provider is None:
            provider = build_javdb_provider()
            self.providers[source_key] = provider
        return provider.get_movie_by_number(movie_number)

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
            board=board,
            period=normalized_period,
        )

        # 批量查本地已有影片：榜单上大多数番号都已入库，命中后直接复用 Movie.id 写榜单条目，
        # 避免逐部走 JavDB 详情接口。watched/want/comment/score 等会变化的热度字段由
        # sync-movie-interactions 任务负责刷新，排行榜任务不再重复拉详情。
        existing_movies: dict[str, Movie] = {}
        if movie_numbers:
            existing_movies = {
                movie.movie_number: movie
                for movie in Movie.select(Movie.id, Movie.movie_number).where(
                    Movie.movie_number.in_(movie_numbers)
                )
            }

        local_hit_count = 0
        imported_count = 0
        skipped_count = 0
        insert_rows: list[dict[str, Any]] = []
        for rank, movie_number in enumerate(movie_numbers, start=1):
            movie = existing_movies.get(movie_number)
            if movie is None:
                # 本地没有的番号才走 JavDB 详情入库
                try:
                    detail = self._get_movie_detail(source_key, movie_number)
                    movie, _created = self.import_service.import_movie_if_missing(detail)
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
            else:
                local_hit_count += 1

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
            "local_hit_movies": local_hit_count,
            "skipped_movies": skipped_count,
            "stored_items": stored_count,
        }

    @staticmethod
    def _scope_has_items(source_key: str, board_key: str, period: str) -> bool:
        return (
            RankingItem.select()
            .where(
                RankingItem.source_key == source_key,
                RankingItem.board_key == board_key,
                RankingItem.period == period,
            )
            .exists()
        )

    @classmethod
    def _iter_sync_targets(
        cls,
        source_keys: tuple[str, ...] | None = None,
    ) -> list[tuple[RankingSourceDefinition, RankingBoardDefinition, str]]:
        # 收敛出本次真正要同步的目标：
        #   should_fetch(period, has_items) 返回 False 则跳过；
        #   账号是否配置、历史年份是否已抓过等前置判断全部由插件回调表达。
        targets: list[tuple[RankingSourceDefinition, RankingBoardDefinition, str]] = []
        for source in RANKING_SOURCES.values():
            if source_keys is not None and source.key not in source_keys:
                continue
            for board in source.boards:
                for period in board_supported_periods(board) or ("",):
                    if (
                        board.should_fetch is not None
                        and not board.should_fetch(
                            period,
                            cls._scope_has_items(source.key, board.key, period),
                        )
                    ):
                        continue
                    targets.append((source, board, period))
        return targets

    def sync_all_rankings(
        self,
        progress_callback=None,
        source_keys: tuple[str, ...] | None = None,
    ) -> dict[str, int]:
        targets = self._iter_sync_targets(source_keys=source_keys)
        stats = {
            "total_targets": len(targets),
            "success_targets": 0,
            "failed_targets": 0,
            "fetched_numbers": 0,
            "imported_movies": 0,
            "local_hit_movies": 0,
            "skipped_movies": 0,
            "stored_items": 0,
        }
        total_targets = len(targets)
        completed_targets = 0
        emit_progress(
            progress_callback,
            current=0,
            total=total_targets,
            text="开始同步排行榜",
            summary_patch=stats,
        )
        for source, board, period in targets:
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
                emit_progress(
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
            stats["local_hit_movies"] += int(target_stats["local_hit_movies"])
            stats["skipped_movies"] += int(target_stats["skipped_movies"])
            stats["stored_items"] += int(target_stats["stored_items"])
            completed_targets += 1
            emit_progress(
                progress_callback,
                current=completed_targets,
                total=total_targets,
                text=f"已同步 {source.name}-{board.name}-{period}",
                summary_patch=stats,
            )
        return stats
