from __future__ import annotations

from typing import Any

from loguru import logger
from src.api.exception.errors import ApiError
from src.common.runtime_time import parse_external_datetime, utc_now_for_db
from src.common.service_helpers import with_movie_card_relations
from sakuramedia_metadata_providers.providers.javdb import JavdbProvider
from src.model import HotReviewItem, Media, Movie, get_database
from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.pagination import PageResponse
from src.schema.discovery import HotReviewListItemResource
from src.service.catalog.catalog_import_service import CatalogImportService

HOT_REVIEW_SOURCE_KEY = "javdb"
HOT_REVIEW_PERIODS = ("weekly", "all", "quarterly", "monthly", "yearly")


class HotReviewCatalogService:
    DEFAULT_PERIOD = "weekly"

    @classmethod
    def _normalize_period(cls, period: str | None) -> str:
        normalized_period = (period or cls.DEFAULT_PERIOD).strip().lower()
        if normalized_period not in HOT_REVIEW_PERIODS:
            raise ApiError(
                422,
                "invalid_hot_review_period",
                "period is not supported",
                {
                    "period": period,
                    "supported_periods": list(HOT_REVIEW_PERIODS),
                },
            )
        return normalized_period

    @classmethod
    def list_items(
        cls,
        period: str | None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[HotReviewListItemResource]:
        normalized_period = cls._normalize_period(period)
        safe_page = max(int(page), 1)
        safe_page_size = max(int(page_size), 1)
        start = (safe_page - 1) * safe_page_size

        base_query = (
            HotReviewItem.select()
            .where(
                HotReviewItem.source_key == HOT_REVIEW_SOURCE_KEY,
                HotReviewItem.period == normalized_period,
            )
            .order_by(HotReviewItem.rank.asc())
        )
        total = base_query.count()
        review_rows = list(base_query.offset(start).limit(safe_page_size))
        if not review_rows:
            return PageResponse[HotReviewListItemResource](
                items=[],
                page=safe_page,
                page_size=safe_page_size,
                total=total,
            )

        movie_ids = [item.movie_id for item in review_rows]
        movie_numbers = [item.movie_number for item in review_rows]
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

        items: list[HotReviewListItemResource] = []
        for review_row in review_rows:
            movie = movies.get(review_row.movie_id)
            if movie is None:
                continue
            movie_item = MovieListItemResource.from_attributes_model(movie)
            movie_item.can_play = movie.movie_number in playable_movie_numbers
            items.append(
                HotReviewListItemResource.model_validate(
                    {
                        "rank": review_row.rank,
                        "review_id": review_row.review_id,
                        "score": review_row.score,
                        "content": review_row.content,
                        "created_at": parse_external_datetime(review_row.review_created_at),
                        "username": review_row.username,
                        "like_count": review_row.like_count,
                        "watch_count": review_row.watch_count,
                        "movie": movie_item.model_dump(),
                    }
                )
            )
        return PageResponse[HotReviewListItemResource](
            items=items,
            page=safe_page,
            page_size=safe_page_size,
            total=total,
        )


class HotReviewSyncService:
    HOT_REVIEW_PAGE_SIZE = 24
    HOT_REVIEW_MAX_PAGES = 100

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
        return build_javdb_provider(use_metadata_proxy=True)

    def _provider_for_source(self, source_key: str) -> Any:
        provider = self.providers.get(source_key)
        if provider is not None:
            return provider
        if source_key == HOT_REVIEW_SOURCE_KEY:
            provider = self._build_javdb_provider()
            self.providers[source_key] = provider
            return provider
        raise ValueError(f"unsupported hot review source: {source_key}")

    def _replace_period_items(
        self,
        source_key: str,
        period: str,
        rows: list[dict[str, Any]],
    ) -> int:
        with get_database().atomic():
            HotReviewItem.delete().where(
                HotReviewItem.source_key == source_key,
                HotReviewItem.period == period,
            ).execute()
            if not rows:
                return 0
            HotReviewItem.insert_many(rows).execute()
            return len(rows)

    def _review_dedup_key(self, review: Any) -> str:
        review_movie = getattr(review, "movie", None)
        movie_number = ((getattr(review_movie, "number", None) or "")).strip()
        review_id = int(getattr(review, "id", 0) or 0)
        if review_id > 0:
            return f"id:{review_id}"
        # 评论 id 缺失时，退化到多字段组合，降低误判概率。
        return (
            "fallback:"
            f"{movie_number}|{getattr(review, 'created_at', '')}|"
            f"{getattr(review, 'username', '')}|{getattr(review, 'content', '')}"
        )

    def sync_period(
        self,
        source_key: str,
        period: str | None,
    ) -> dict[str, int | str]:
        if source_key != HOT_REVIEW_SOURCE_KEY:
            raise ValueError(f"unsupported hot review source: {source_key}")
        normalized_period = HotReviewCatalogService._normalize_period(period)
        provider = self._provider_for_source(source_key)
        now = utc_now_for_db()
        fetched_reviews = 0
        imported_movies = 0
        skipped_reviews = 0
        seen_review_keys: set[str] = set()
        movie_by_number: dict[str, Movie | None] = {}
        insert_rows: list[dict[str, Any]] = []
        rank_cursor = 0

        for page in range(1, self.HOT_REVIEW_MAX_PAGES + 1):
            page_reviews = provider.get_hot_reviews(
                period=normalized_period,
                page=page,
                limit=self.HOT_REVIEW_PAGE_SIZE,
            )
            if not page_reviews:
                break

            new_page_reviews = 0
            for review in page_reviews:
                review_key = self._review_dedup_key(review)
                if review_key in seen_review_keys:
                    continue
                seen_review_keys.add(review_key)
                new_page_reviews += 1
                fetched_reviews += 1
                rank_cursor += 1

                review_movie = getattr(review, "movie", None)
                movie_number = ((getattr(review_movie, "number", None) or "")).strip()
                if not movie_number:
                    skipped_reviews += 1
                    logger.warning(
                        "Hot review sync item skipped because movie number is empty source_key={} period={} rank={} review_id={}",
                        source_key,
                        normalized_period,
                        rank_cursor,
                        getattr(review, "id", 0),
                    )
                    continue

                if movie_number not in movie_by_number:
                    try:
                        # 每抓到一条评论即尝试影片入库，避免先全量抓完再入库。
                        detail = provider.get_movie_by_number(movie_number)
                        movie_by_number[movie_number] = self.import_service.upsert_movie_from_javdb_detail(detail)
                        imported_movies += 1
                    except Exception as exc:
                        movie_by_number[movie_number] = None
                        logger.warning(
                            "Hot review sync movie import failed source_key={} period={} movie_number={} detail={}",
                            source_key,
                            normalized_period,
                            movie_number,
                            exc,
                        )

                movie = movie_by_number.get(movie_number)
                if movie is None:
                    skipped_reviews += 1
                    logger.warning(
                        "Hot review sync item skipped source_key={} period={} rank={} review_id={} movie_number={}",
                        source_key,
                        normalized_period,
                        rank_cursor,
                        getattr(review, "id", 0),
                        movie_number,
                    )
                    continue
                review_created_at = getattr(review, "created_at", None)
                insert_rows.append(
                    {
                        "source_key": source_key,
                        "period": normalized_period,
                        "rank": rank_cursor,
                        "review_id": getattr(review, "id", 0),
                        "movie_number": movie.movie_number,
                        "movie": movie.id,
                        "score": getattr(review, "score", 0),
                        "content": getattr(review, "content", "") or "",
                        "review_created_at": (
                            review_created_at.isoformat(timespec="seconds")
                            if review_created_at is not None
                            else None
                        ),
                        "username": getattr(review, "username", "") or "",
                        "like_count": getattr(review, "like_count", 0),
                        "watch_count": getattr(review, "watch_count", 0),
                        "created_at": now,
                        "updated_at": now,
                    }
                )

            # 若当前页没有新增评论，说明分页已循环，提前停止。
            if new_page_reviews == 0:
                logger.warning(
                    "Hot review sync stopped because page has no new reviews source_key={} period={} page={}",
                    source_key,
                    normalized_period,
                    page,
                )
                break
            if len(page_reviews) < self.HOT_REVIEW_PAGE_SIZE:
                break
        else:
            logger.warning(
                "Hot review sync stopped by max pages source_key={} period={} max_pages={} page_size={}",
                source_key,
                normalized_period,
                self.HOT_REVIEW_MAX_PAGES,
                self.HOT_REVIEW_PAGE_SIZE,
            )

        stored_count = self._replace_period_items(
            source_key=source_key,
            period=normalized_period,
            rows=insert_rows,
        )
        return {
            "source_key": source_key,
            "period": normalized_period,
            "fetched_reviews": fetched_reviews,
            "imported_movies": imported_movies,
            "skipped_reviews": skipped_reviews,
            "stored_items": stored_count,
        }

    def sync_all_hot_reviews(self) -> dict[str, int]:
        stats = {
            "total_periods": 0,
            "success_periods": 0,
            "failed_periods": 0,
            "fetched_reviews": 0,
            "imported_movies": 0,
            "skipped_reviews": 0,
            "stored_items": 0,
        }
        for period in HOT_REVIEW_PERIODS:
            stats["total_periods"] += 1
            try:
                target_stats = self.sync_period(
                    source_key=HOT_REVIEW_SOURCE_KEY,
                    period=period,
                )
            except Exception as exc:
                stats["failed_periods"] += 1
                logger.warning(
                    "Hot review sync period failed source_key={} period={} detail={}",
                    HOT_REVIEW_SOURCE_KEY,
                    period,
                    exc,
                )
                continue
            stats["success_periods"] += 1
            stats["fetched_reviews"] += int(target_stats["fetched_reviews"])
            stats["imported_movies"] += int(target_stats["imported_movies"])
            stats["skipped_reviews"] += int(target_stats["skipped_reviews"])
            stats["stored_items"] += int(target_stats["stored_items"])
        return stats
