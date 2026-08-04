from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import peewee
from loguru import logger

from src.api.exception.errors import ApiError
from src.common.runtime_time import runtime_now, utc_now_for_db
from src.common.service_helpers import (
    parse_special_tags_text,
    with_movie_card_relations,
)
from src.config.config import settings
from src.metadata._providers.exceptions import JavdbAuthError
from src.metadata._providers.javdb import JavdbProvider
from src.model import Media, Movie, RankingItem, get_database
from src.schema.catalog.movies import MovieListItemResource
from src.schema.discovery import (
    RankedMovieListItemResource,
    RankingBoardItemsResource,
    RankingBoardResource,
    RankingSourceResource,
)
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.discovery.ranking_notifications import (
    create_ranking_account_error_notification,
)


@dataclass(frozen=True)
class RankingBoardDefinition:
    key: str
    name: str
    provider_raw_key: str
    supported_periods: tuple[str, ...]
    default_period: str | None = None
    # 抓取方式，决定走 JavDB 哪个接口；仅用于抓取分发，不影响 list_boards/list_sources。
    #   rankings -> /api/v1/rankings（免登录，type=0/1/3）
    #   playback -> /api/v1/rankings/playback（免登录，filter_by=all/high_score）
    #   top250   -> /api/v1/movies/top（需登录，period 编码 all/uncensored/censored/fc2/年份）
    fetch_kind: str = "rankings"


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


# TOP250（需登录）：board 固定为 top250，period 编码不同子榜——
# 固定子榜 all/无码/有码/FC2，外加 2008 至当前年的逐年年度榜。
TOP250_FIXED_PERIODS = ("all", "uncensored", "censored", "fc2")
TOP250_START_YEAR = 2008
# 固定子榜 period -> playback 接口的 (type, type_value)；年份 period 走 type=year。
_TOP250_VIDEO_TYPE_BY_PERIOD = {"censored": "0", "uncensored": "1", "fc2": "3"}


def _current_year() -> int:
    return runtime_now().year


def _top250_year_periods() -> tuple[str, ...]:
    # 当前年在前，回溯到起始年；逐年滚动，无需每年改代码。
    return tuple(str(year) for year in range(_current_year(), TOP250_START_YEAR - 1, -1))


def board_supported_periods(board: RankingBoardDefinition) -> tuple[str, ...]:
    # top250 的年份维度是动态的，其余 board 直接用静态 supported_periods。
    if board.fetch_kind == "top250":
        return tuple(board.supported_periods) + _top250_year_periods()
    return board.supported_periods


def top250_type_for_period(period: str) -> tuple[str, str]:
    # 把 top250 的 period 映射成 /api/v1/movies/top 的 (type, type_value)。
    if period == "all":
        return ("all", "")
    if period in _TOP250_VIDEO_TYPE_BY_PERIOD:
        return ("video_type", _TOP250_VIDEO_TYPE_BY_PERIOD[period])
    return ("year", period)


def _is_top250_historical_year(period: str) -> bool:
    # 历史年份（非当前年）：抓一次即可，库里已有就不再每天重抓。
    return period.isdigit() and int(period) < _current_year()


def top250_historical_year_periods() -> list[str]:
    # 静态历史年份 period 列表（不含当前年），供跨模块判定"这条 RankingItem 已经不会再变"。
    # 与 _is_top250_historical_year 判定共用同一批年份，避免各处重复展开区间。
    return [str(year) for year in range(TOP250_START_YEAR, _current_year())]


JAVDB_SOURCE = RankingSourceDefinition(
    key="javdb",
    name="JavDB",
    boards=(
        # 播放榜排在最前，使整体同步优先抓取；provider_raw_key 对应 playback 接口的 filter_by。
        RankingBoardDefinition(
            key="playback_all",
            name="热播",
            provider_raw_key="all",
            supported_periods=("daily", "weekly", "monthly"),
            default_period="daily",
            fetch_kind="playback",
        ),
        RankingBoardDefinition(
            key="playback_high_score",
            name="高评分",
            provider_raw_key="high_score",
            supported_periods=("daily", "weekly", "monthly"),
            default_period="daily",
            fetch_kind="playback",
        ),
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
        # TOP250（需登录）排最后：首次历史回填量大，避免阻塞上面更轻量、更新更频繁的榜单。
        # period 维度由 board_supported_periods 动态补充年份；provider_raw_key 不使用。
        RankingBoardDefinition(
            key="top250",
            name="TOP250",
            provider_raw_key="",
            supported_periods=TOP250_FIXED_PERIODS,
            default_period="all",
            fetch_kind="top250",
        ),
    ),
)

RANKING_SOURCES: dict[str, RankingSourceDefinition] = {
    JAVDB_SOURCE.key: JAVDB_SOURCE,
}


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

        order_expressions, needs_movie_join = cls._build_board_items_sort(sort)
        base_query = RankingItem.select().where(
            RankingItem.source_key == source_key,
            RankingItem.board_key == board_key,
            RankingItem.period == normalized_period,
        )
        if needs_movie_join:
            # 按 Movie.heat 排序时 JOIN Movie 表
            base_query = base_query.join(
                Movie, on=(RankingItem.movie == Movie.id)
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
        # 对齐 recommendation_service 权威版：一次性取 media 的 special_tags，
        # 同时算出可播放与 4K 番号集合，避免 is_4k 恒为 false。
        playable_movie_numbers: set[str] = set()
        is_4k_movie_numbers: set[str] = set()
        media_rows = (
            Media.select(Media.movie, Media.special_tags)
            .where(
                Media.valid == True,
                Media.movie.in_(movie_numbers),
            )
            .tuples()
        )
        for movie_number, special_tags in media_rows:
            playable_movie_numbers.add(movie_number)
            if "4K" in parse_special_tags_text(special_tags):
                is_4k_movie_numbers.add(movie_number)

        items: list[RankedMovieListItemResource] = []
        for ranking_row in ranking_rows:
            movie = movies.get(ranking_row.movie_id)
            if movie is None:
                continue
            movie_item = MovieListItemResource.from_attributes_model(movie)
            movie_item.can_play = movie.movie_number in playable_movie_numbers
            movie_item.is_4k = movie.movie_number in is_4k_movie_numbers
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

    @staticmethod
    def _build_javdb_provider() -> JavdbProvider:
        from src.metadata.factory import build_javdb_provider
        return build_javdb_provider()

    def _provider_for_source(self, source_key: str) -> Any:
        provider = self.providers.get(source_key)
        if provider is not None:
            return provider
        if source_key == "javdb":
            provider = self._build_javdb_provider()
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
            # 按 fetch_kind 分发到不同 JavDB 接口。
            if board.fetch_kind == "top250":
                top_type, type_value = top250_type_for_period(period)
                return provider.get_top_numbers(top_type=top_type, type_value=type_value)
            if board.fetch_kind == "playback":
                return provider.get_playback_rank_numbers(
                    filter_by=board.provider_raw_key,
                    period=period,
                )
            return provider.get_rank_numbers(
                video_type=board.provider_raw_key,
                period=period,
            )
        raise ValueError(f"unsupported ranking source: {source_key}")

    def _get_movie_detail(self, source_key: str, movie_number: str) -> Any:
        if source_key == "javdb":
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
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

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
    ) -> list[tuple[RankingSourceDefinition, RankingBoardDefinition, str]]:
        # 收敛出本次真正要同步的目标，应用两条策略：
        #   1) top250 需登录：未配账号则整 board 跳过；
        #   2) top250 历史年份抓一次：库里已有数据就不再每天重抓（当前年与固定子榜照常抓）。
        account_configured = settings.metadata.javdb_account_configured
        targets: list[tuple[RankingSourceDefinition, RankingBoardDefinition, str]] = []
        for source in RANKING_SOURCES.values():
            for board in source.boards:
                requires_account = board.fetch_kind == "top250"
                if requires_account and not account_configured:
                    continue
                for period in board_supported_periods(board) or ("",):
                    if (
                        board.fetch_kind == "top250"
                        and _is_top250_historical_year(period)
                        and cls._scope_has_items(source.key, board.key, period)
                    ):
                        continue
                    targets.append((source, board, period))
        return targets

    def sync_all_rankings(
        self,
        progress_callback=None,
        task_run_id: int | None = None,
    ) -> dict[str, int]:
        targets = self._iter_sync_targets()
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
        # 登录失败只发一次通知，并跳过其余需登录的目标。
        account_login_notified = False
        self._emit_progress(
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
            except JavdbAuthError as exc:
                # 登录失败：仅记日志、发一次通知、跳过该目标，不计入 failed_targets，
                # 避免触发额外的“已完成但有失败”自动 warning，保证只有一条通知。
                logger.warning(
                    "Ranking sync skipped due to javdb auth failure board_key={} period={} detail={}",
                    board.key,
                    period,
                    exc,
                )
                if not account_login_notified:
                    account_login_notified = True
                    self._notify_account_login_failed(task_run_id)
                completed_targets += 1
                self._emit_progress(
                    progress_callback,
                    current=completed_targets,
                    total=total_targets,
                    text=f"JavDB 账号登录失败，跳过 {source.name}-{board.name}-{period}",
                    summary_patch=stats,
                )
                continue
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
            stats["local_hit_movies"] += int(target_stats["local_hit_movies"])
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

    @staticmethod
    def _notify_account_login_failed(task_run_id: int | None) -> None:
        try:
            create_ranking_account_error_notification(
                related_task_run_id=task_run_id,
            )
        except Exception as exc:
            # 通知失败不应影响主同步流程。
            logger.warning("Create ranking account error notification failed detail={}", exc)
