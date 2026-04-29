"""影片目录 service。

负责影片列表、详情组装、订阅状态维护，以及按影片编号从 JavDB 拉取并落库。
阅读入口建议从 ``list_movies`` / ``get_movie_detail`` / ``stream_search_and_upsert_movie_from_javdb`` 开始，
再回看查询 helper 和本地资源组装 helper。
"""

from datetime import datetime
from queue import Queue
from threading import Thread
from typing import Dict, Iterator, List, Optional, Sequence

from loguru import logger
from peewee import JOIN, fn

from src.api.exception.errors import ApiError
from src.common.service_helpers import (
    media_special_tag_match_expression,
    playable_exists_expression,
    require_record,
    with_movie_card_relations,
)
from src.common import (
    build_signed_media_url,
    normalize_movie_number,
    parse_movie_number_from_path,
)
from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from sakuramedia_metadata_providers.exceptions import (
    MissavThumbnailNotFoundError,
    MissavThumbnailRequestError,
)
from sakuramedia_metadata_providers.providers.javdb import JavdbProvider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import (
    Actor,
    Image,
    Media,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSeries,
    MovieTag,
    Tag,
)
from src.model.base import get_database
from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import (
    MOVIE_LIST_SORT_FIELDS,
    MovieCollectionMarkResponse,
    MovieCollectionMarkType,
    MovieCollectionStatusResource,
    MovieCollectionType,
    MovieActorResource,
    MovieMediaPointResource,
    MovieMediaProgressResource,
    MovieMediaResource,
    MovieDetailResource,
    MovieListItemResource,
    MovieListStatus,
    MovieSpecialTagFilter,
    MissavThumbnailResource,
    MovieNumberParseResponse,
    MovieReviewSort,
    TagResource,
)
from src.schema.common.pagination import PageResponse
from sakuramedia_metadata_providers.models import JavdbMovieDetailResource, JavdbMovieReviewResource
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError
from src.service.catalog.movie_desc_translation_client import MovieDescTranslationClientError
from src.service.catalog.movie_desc_translation_service import (
    MovieDescTranslationService,
    MovieDescTranslationTaskAbortError,
)
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.catalog.movie_interaction_sync_service import MovieInteractionSyncService
from src.service.catalog.missav_thumbnail_service import MissavThumbnailService
from src.service.collections import PlaylistService
from src.service.system.activity_service import ActivityService


class MovieService:
    """聚合 Movie 相关查询、详情拼装和远端导入流程。"""

    MOVIE_LIST_NULLABLE_SORT_FIELDS = {"release_date", "subscribed_at"}
    MOVIE_LIST_SORT_FIELD_MAP = {
        "release_date": Movie.release_date,
        "added_at": Movie.id,
        "subscribed_at": Movie.subscribed_at,
        "comment_count": Movie.comment_count,
        "score_number": Movie.score_number,
        "want_watch_count": Movie.want_watch_count,
        "heat": Movie.heat,
    }

    _playable_exists_expression = staticmethod(playable_exists_expression)

    @staticmethod
    def _media_exists_expression(*conditions):
        media_query = Media.select(Media.id).where(
            Media.movie == Movie.movie_number,
            *conditions,
        )
        return fn.EXISTS(media_query)

    @classmethod
    def _special_tag_exists_expression(cls, media_tag: str):
        return cls._media_exists_expression(
            Media.valid == True,
            media_special_tag_match_expression(media_tag),
        )

    @classmethod
    def _filtered_movies(
        cls,
        actor_id: Optional[int] = None,
        tag_ids: list[int] | None = None,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        special_tag: MovieSpecialTagFilter | None = None,
        series_id: int | None = None,
        director_name: str | None = None,
        maker_name: str | None = None,
    ):
        """构建影片列表的基础筛选链路，供列表和计数查询复用。"""
        query = Movie.select()
        if actor_id is None:
            filtered_query = query
        else:
            movie_ids = MovieActor.select(MovieActor.movie).where(MovieActor.actor == actor_id)
            filtered_query = query.where(Movie.id.in_(movie_ids))

        if tag_ids is not None:
            # 标签筛选走子查询，避免主查询 join 后出现重复影片和 total 偏差。
            tagged_movie_ids = MovieTag.select(MovieTag.movie).where(MovieTag.tag.in_(tag_ids))
            filtered_query = filtered_query.where(Movie.id.in_(tagged_movie_ids))

        if year is not None:
            year_start = datetime(year, 1, 1)
            year_end = datetime(year + 1, 1, 1)
            filtered_query = filtered_query.where(
                Movie.release_date >= year_start,
                Movie.release_date < year_end,
            )

        if status == MovieListStatus.SUBSCRIBED:
            filtered_query = filtered_query.where(Movie.is_subscribed == True)
        elif status == MovieListStatus.PLAYABLE:
            filtered_query = filtered_query.where(cls._playable_exists_expression())

        if collection_type == MovieCollectionType.SINGLE:
            filtered_query = filtered_query.where(Movie.is_collection == False)
        if special_tag is not None:
            filtered_query = filtered_query.where(
                cls._special_tag_exists_expression(special_tag.to_media_tag())
            )
        if series_id is not None:
            # 系列影片查询统一使用本地 movie_series.id，避免系列名变更导致匹配不稳定。
            filtered_query = filtered_query.where(Movie.series == series_id)
        if director_name is not None:
            filtered_query = filtered_query.where(Movie.director_name == director_name)
        if maker_name is not None:
            filtered_query = filtered_query.where(Movie.maker_name == maker_name)
        return filtered_query

    @classmethod
    def _build_movie_list_sort(cls, sort: Optional[str]) -> Sequence:
        """解析 ``field:direction`` 排序表达式，并补上稳定的次级排序。"""
        if sort is None:
            return [Movie.movie_number.asc()]

        normalized = sort.strip().lower()
        if not normalized:
            return [Movie.movie_number.asc()]

        try:
            field_name, direction = normalized.split(":", 1)
        except ValueError:
            raise ApiError(
                422,
                "invalid_movie_filter",
                "Invalid sort expression",
                {"sort": sort},
            )

        if field_name not in MOVIE_LIST_SORT_FIELDS or direction not in ("asc", "desc"):
            raise ApiError(
                422,
                "invalid_movie_filter",
                "Invalid sort expression",
                {"sort": sort},
            )

        sort_field = cls.MOVIE_LIST_SORT_FIELD_MAP[field_name]
        ordered_field = sort_field.asc() if direction == "asc" else sort_field.desc()
        tie_breaker = Movie.id.asc() if direction == "asc" else Movie.id.desc()
        if field_name in cls.MOVIE_LIST_NULLABLE_SORT_FIELDS:
            # 允许空值的字段统一放到后面，避免不同数据库里空值排序行为不一致。
            return [sort_field.is_null(), ordered_field, tie_breaker]
        return [ordered_field, tie_breaker]

    @classmethod
    def _movie_list_query(
        cls,
        actor_id: Optional[int] = None,
        tag_ids: list[int] | None = None,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        special_tag: MovieSpecialTagFilter | None = None,
        sort: Optional[str] = None,
        series_id: int | None = None,
        director_name: str | None = None,
        maker_name: str | None = None,
    ):
        """列表查询统一在这里补齐封面图和 ``can_play`` 计算列。"""
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        is_4k_expression = cls._special_tag_exists_expression("4K").alias("is_4k")
        query, _thin_cover_alias = with_movie_card_relations(
            cls._filtered_movies(
                actor_id,
                tag_ids,
                year,
                status,
                collection_type,
                special_tag,
                series_id,
                director_name,
                maker_name,
            ).select(Movie, can_play_expression, is_4k_expression)
        )
        return query.order_by(*cls._build_movie_list_sort(sort))

    @classmethod
    def _latest_movies_query(cls):
        """按最近导入媒体时间倒序列出影片，而不是按影片自身创建时间。"""
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        is_4k_expression = cls._special_tag_exists_expression("4K").alias("is_4k")
        latest_media_created_at = fn.MAX(Media.created_at)
        query, thin_cover_alias = with_movie_card_relations(
            Movie.select(Movie, can_play_expression, is_4k_expression)
            .join(Media)
            .switch(Movie)
        )
        return (
            query
            .group_by(Movie.id, Image.id, thin_cover_alias.id, MovieSeries.id)
            .order_by(latest_media_created_at.desc(), Movie.id.desc())
        )

    @classmethod
    def _subscribed_actor_latest_movies_query(cls):
        """列出至少关联一位已订阅演员的影片，按上映日期倒序。"""
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        is_4k_expression = cls._special_tag_exists_expression("4K").alias("is_4k")
        query, thin_cover_alias = with_movie_card_relations(
            Movie.select(Movie, can_play_expression, is_4k_expression)
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .join(Actor, JOIN.INNER, on=(MovieActor.actor == Actor.id))
            .switch(Movie)
        )
        return (
            query
            # 订阅演员最新影片接口默认排除合集番号。
            .where(Actor.is_subscribed == True, Movie.is_collection == False)
            .group_by(Movie.id, Image.id, thin_cover_alias.id, MovieSeries.id)
            .order_by(Movie.release_date.is_null(), Movie.release_date.desc(), Movie.id.desc())
        )

    @staticmethod
    def _subscribed_actor_movies_query():
        """查询至少关联一位已订阅演员的去重影片。"""
        return (
            Movie.select(Movie.id)
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .join(Actor, JOIN.INNER, on=(MovieActor.actor == Actor.id))
            # total 口径与列表一致，默认排除合集番号。
            .where(Actor.is_subscribed == True, Movie.is_collection == False)
            .distinct()
        )

    @staticmethod
    def _normalized_movie_number_expression():
        """把库内编号归一化成和搜索输入一致的比较格式。"""
        normalized = fn.UPPER(fn.TRIM(Movie.movie_number))
        normalized = fn.REPLACE(normalized, " ", "")
        normalized = fn.REPLACE(normalized, "_", "-")
        normalized = fn.REPLACE(normalized, "PPV-", "")
        return normalized

    @staticmethod
    def _build_javdb_provider() -> JavdbProvider:
        from src.metadata.factory import build_javdb_provider
        return build_javdb_provider()

    @classmethod
    def _build_catalog_import_service(cls) -> CatalogImportService:
        return CatalogImportService()

    @staticmethod
    def _build_movie_desc_translation_service() -> MovieDescTranslationService:
        return MovieDescTranslationService()

    @staticmethod
    def _build_movie_interaction_sync_service() -> MovieInteractionSyncService:
        return MovieInteractionSyncService()

    @staticmethod
    def _build_missav_thumbnail_service() -> MissavThumbnailService:
        return MissavThumbnailService()

    @staticmethod
    def _require_movie(movie_number: str) -> Movie:
        return require_record(
            Movie, Movie.movie_number == movie_number,
            error_code="movie_not_found",
            error_message="影片不存在",
            error_details={"movie_number": movie_number},
        )

    @classmethod
    def _require_movie_by_normalized_number(cls, movie_number: str) -> tuple[Movie, str]:
        normalized_movie_number = normalize_movie_number(movie_number)
        if not normalized_movie_number:
            raise ApiError(404, "movie_not_found", "影片不存在", {"movie_number": movie_number})

        movie = (
            Movie.select(Movie)
            .where(cls._normalized_movie_number_expression() == normalized_movie_number)
            .get_or_none()
        )
        if movie is None:
            raise ApiError(404, "movie_not_found", "影片不存在", {"movie_number": movie_number})
        return movie, normalized_movie_number

    @staticmethod
    def _build_movie_metadata_refresh_error_details(
        *,
        movie_number: str,
        normalized_movie_number: str,
        detail: str,
    ) -> dict[str, str]:
        return {
            "movie_number": movie_number,
            "normalized_movie_number": normalized_movie_number,
            "detail": detail,
        }

    @classmethod
    def _raise_movie_metadata_refresh_failed(
        cls,
        *,
        movie_number: str,
        normalized_movie_number: str,
        exc: Exception,
        log_message: str | None = None,
    ) -> None:
        if log_message:
            logger.exception(
                log_message,
                movie_number,
                normalized_movie_number,
                exc,
            )
        raise ApiError(
            502,
            "movie_metadata_refresh_failed",
            "影片元数据刷新失败",
            cls._build_movie_metadata_refresh_error_details(
                movie_number=movie_number,
                normalized_movie_number=normalized_movie_number,
                detail=str(exc),
            ),
        ) from exc

    @classmethod
    def _fetch_remote_movie_metadata(
        cls,
        *,
        movie: Movie,
        normalized_movie_number: str,
    ) -> JavdbMovieDetailResource:
        try:
            return cls._build_javdb_provider().get_movie_by_number(normalized_movie_number)
        except MetadataNotFoundError as exc:
            raise ApiError(
                404,
                "movie_metadata_not_found",
                "影片远端元数据不存在",
                {"movie_number": movie.movie_number, "normalized_movie_number": normalized_movie_number},
            ) from exc
        except MetadataRequestError as exc:
            cls._raise_movie_metadata_refresh_failed(
                movie_number=movie.movie_number,
                normalized_movie_number=normalized_movie_number,
                exc=exc,
            )
        except Exception as exc:
            cls._raise_movie_metadata_refresh_failed(
                movie_number=movie.movie_number,
                normalized_movie_number=normalized_movie_number,
                exc=exc,
                log_message="Movie metadata fetch failed movie_number={} normalized={} detail={}",
            )

    @classmethod
    def _validate_remote_movie_metadata_number(
        cls,
        *,
        movie: Movie,
        detail: JavdbMovieDetailResource,
    ) -> str:
        local_normalized_movie_number = normalize_movie_number(movie.movie_number)
        remote_normalized_movie_number = normalize_movie_number(detail.movie_number)
        # 严格要求远端详情与本地影片指向同一番号，避免误把相邻作品覆盖到当前记录。
        if not remote_normalized_movie_number or remote_normalized_movie_number != local_normalized_movie_number:
            raise ApiError(
                409,
                "movie_metadata_number_conflict",
                "远端元数据番号与本地影片不一致",
                {
                    "movie_number": movie.movie_number,
                    "normalized_movie_number": local_normalized_movie_number,
                    "remote_movie_number": detail.movie_number,
                    "remote_normalized_movie_number": remote_normalized_movie_number,
                },
            )
        return local_normalized_movie_number

    @classmethod
    def _validate_remote_movie_metadata_javdb_id(
        cls,
        *,
        movie: Movie,
        detail: JavdbMovieDetailResource,
        normalized_movie_number: str,
    ) -> None:
        remote_javdb_id = (detail.javdb_id or "").strip()
        current_javdb_id = (movie.javdb_id or "").strip()
        if not remote_javdb_id or remote_javdb_id == current_javdb_id:
            return

        conflicting_movie = (
            Movie.select(Movie.movie_number)
            .where(
                (Movie.javdb_id == remote_javdb_id)
                & (Movie.id != movie.id)
            )
            .get_or_none()
        )
        if conflicting_movie is None:
            return

        # 远端主键已被其他本地影片占用时直接拒绝刷新，避免覆盖错片。
        raise ApiError(
            409,
            "movie_metadata_javdb_id_conflict",
            "远端元数据 JavDB ID 与其他本地影片冲突",
            {
                "movie_number": movie.movie_number,
                "normalized_movie_number": normalized_movie_number,
                "current_javdb_id": current_javdb_id,
                "remote_javdb_id": remote_javdb_id,
                "conflicting_movie_number": conflicting_movie.movie_number,
            },
        )

    @staticmethod
    def _list_movie_media(movie: Movie) -> List[Media]:
        return list(
            Media.select(Media)
            .where(Media.movie == movie)
            .order_by(Media.id)
        )

    @staticmethod
    def _actors(movie: Movie) -> List[Actor]:
        return list(
            Actor.select(Actor, Image)
            .join(Image, JOIN.LEFT_OUTER, on=(Actor.profile_image == Image.id))
            .join(MovieActor, JOIN.INNER, on=(MovieActor.actor == Actor.id))
            .where(MovieActor.movie == movie)
            .order_by(Actor.id)
        )

    @staticmethod
    def _plot_images(movie: Movie) -> List[Image]:
        query = (
            MoviePlotImage.select(MoviePlotImage, Image)
            .join(Image)
            .where(MoviePlotImage.movie == movie)
            .order_by(MoviePlotImage.id)
        )
        return [link.image for link in query]

    @staticmethod
    def _media_items(movie: Movie) -> List[MovieMediaResource]:
        """把媒体、播放进度和打点信息折叠成详情页需要的资源结构。"""
        media_items = list(
            Media.select(Media)
            .where(Media.movie == movie)
            .order_by(Media.id)
        )
        if not media_items:
            return []

        media_ids = [media.id for media in media_items]
        # 进度和打点分开查，避免在一个大 join 里把媒体行放大成笛卡尔展开。
        progress_items = {
            progress.media_id: progress
            for progress in MediaProgress.select(MediaProgress).where(MediaProgress.media.in_(media_ids))
        }

        points_by_media_id: Dict[int, List[MovieMediaPointResource]] = {}
        point_query = (
            MediaPoint.select(MediaPoint, MediaThumbnail, Image)
            .join(MediaThumbnail)
            .switch(MediaThumbnail)
            .join(Image)
            .where(MediaPoint.media.in_(media_ids))
            .order_by(MediaPoint.media, MediaPoint.id)
        )
        for point in point_query:
            if point.media_id not in points_by_media_id:
                points_by_media_id[point.media_id] = []
            points_by_media_id[point.media_id].append(
                MovieMediaPointResource(
                    point_id=point.id,
                    thumbnail_id=point.thumbnail_id,
                    offset_seconds=point.offset_seconds,
                    image=ImageResource.from_attributes_model(point.thumbnail.image),
                )
            )

        resources: List[MovieMediaResource] = []
        for media in media_items:
            # 详情资源需要把播放进度和精彩时间点挂回各自 media 上。
            progress = progress_items.get(media.id)
            if progress is None:
                media.progress = None
            else:
                media.progress = MovieMediaProgressResource(
                    last_position_seconds=progress.position_seconds,
                    last_watched_at=progress.last_watched_at,
                )
            media.points = points_by_media_id.get(media.id, [])
            media.play_url = build_signed_media_url(media.id)
            resources.append(MovieMediaResource.from_attributes_model(media))
        return resources

    @staticmethod
    def get_movie_detail(movie_number: str) -> MovieDetailResource:
        """组装影片详情页所需的所有关联资源。"""
        is_4k_expression = MovieService._special_tag_exists_expression("4K").alias("is_4k")
        query, _thin_cover_alias = with_movie_card_relations(
            Movie.select(Movie, is_4k_expression)
        )
        movie = (
            query
            .where(Movie.movie_number == movie_number)
            .get_or_none()
        )
        if movie is None:
            raise ApiError(404, "movie_not_found", "影片不存在", {"movie_number": movie_number})

        # 标签、演员、剧照、媒体都按详情页独立查询，避免一次 join 带来重复行和复杂去重。
        tags = [
            TagResource(tag_id=tag.id, name=tag.name)
            for tag in Tag.select(Tag).join(MovieTag).where(MovieTag.movie == movie).order_by(Tag.id)
        ]

        movie.actors = MovieService._actors(movie)
        movie.tags = tags
        movie.plot_images = MovieService._plot_images(movie)
        movie.media_items = MovieService._media_items(movie)
        movie.playlists = PlaylistService.list_movie_playlists(movie)
        movie.can_play = any(media_item.valid for media_item in movie.media_items)
        return MovieDetailResource.from_attributes_model(movie)

    @staticmethod
    def list_movies(
        actor_id: Optional[int] = None,
        tag_ids: list[int] | None = None,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        special_tag: MovieSpecialTagFilter | None = None,
        sort: Optional[str] = None,
        director_name: str | None = None,
        maker_name: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MovieListItemResource]:
        start = max(page - 1, 0) * page_size
        total = MovieService._filtered_movies(
            actor_id,
            tag_ids,
            year,
            status,
            collection_type,
            special_tag,
            None,
            director_name,
            maker_name,
        ).count()
        movies = list(
            MovieService._movie_list_query(
                actor_id,
                tag_ids,
                year,
                status,
                collection_type,
                special_tag,
                sort,
                None,
                director_name,
                maker_name,
            ).offset(start).limit(page_size)
        )
        return PageResponse[MovieListItemResource](
            items=MovieListItemResource.from_items(movies),
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def list_movies_by_series(
        series_id: int,
        sort: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MovieListItemResource]:
        start = max(page - 1, 0) * page_size
        total = MovieService._filtered_movies(series_id=series_id).count()
        movies = list(
            MovieService._movie_list_query(series_id=series_id, sort=sort)
            .offset(start)
            .limit(page_size)
        )
        return PageResponse[MovieListItemResource](
            items=MovieListItemResource.from_items(movies),
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def list_latest_movies(
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MovieListItemResource]:
        start = max(page - 1, 0) * page_size
        total = Movie.select(Movie.id).join(Media).group_by(Movie.id).count()
        movies = list(MovieService._latest_movies_query().offset(start).limit(page_size))
        return PageResponse[MovieListItemResource](
            items=MovieListItemResource.from_items(movies),
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def list_subscribed_actor_latest_movies(
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MovieListItemResource]:
        start = max(page - 1, 0) * page_size
        total = MovieService._subscribed_actor_movies_query().count()
        movies = list(
            MovieService._subscribed_actor_latest_movies_query().offset(start).limit(page_size)
        )
        return PageResponse[MovieListItemResource](
            items=MovieListItemResource.from_items(movies),
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def parse_movie_number_query(query: str) -> MovieNumberParseResponse:
        parsed_movie_number = parse_movie_number_from_path(query.strip())
        if not parsed_movie_number:
            return MovieNumberParseResponse(
                query=query,
                parsed=False,
                movie_number=None,
                reason="movie_number_not_found",
            )
        return MovieNumberParseResponse(
            query=query,
            parsed=True,
            movie_number=parsed_movie_number,
            reason=None,
        )

    @classmethod
    def search_local_movies(cls, movie_number: str) -> List[MovieListItemResource]:
        normalized_movie_number = normalize_movie_number(movie_number)
        if not normalized_movie_number:
            return []
        # 本地搜索只取最匹配的一条，职责是回答“库里有没有这个番号”。
        movies = list(
            cls._movie_list_query().where(
                cls._normalized_movie_number_expression() == normalized_movie_number
            ).limit(1)
        )
        return MovieListItemResource.from_items(movies)

    @classmethod
    def get_movie_collection_status(cls, movie_number: str) -> MovieCollectionStatusResource:
        normalized_movie_number = normalize_movie_number(movie_number)
        if not normalized_movie_number:
            raise ApiError(404, "movie_not_found", "影片不存在", {"movie_number": movie_number})

        # 与本地搜索保持同一套标准化匹配，确保不同输入格式能命中同一影片。
        movie = (
            Movie.select(Movie.movie_number, Movie.is_collection)
            .where(cls._normalized_movie_number_expression() == normalized_movie_number)
            .get_or_none()
        )
        if movie is None:
            raise ApiError(404, "movie_not_found", "影片不存在", {"movie_number": movie_number})

        return MovieCollectionStatusResource(
            movie_number=movie.movie_number,
            is_collection=bool(movie.is_collection),
        )

    @classmethod
    def mark_movie_collection_type(
        cls,
        movie_numbers: List[str],
        collection_type: MovieCollectionMarkType,
    ) -> MovieCollectionMarkResponse:
        requested_count = len(movie_numbers)
        normalized_movie_numbers: list[str] = []
        seen_numbers: set[str] = set()
        for movie_number in movie_numbers:
            normalized = normalize_movie_number(movie_number)
            if not normalized or normalized in seen_numbers:
                continue
            seen_numbers.add(normalized)
            normalized_movie_numbers.append(normalized)

        if not normalized_movie_numbers:
            return MovieCollectionMarkResponse(
                requested_count=requested_count,
                updated_count=0,
            )

        matched_movies = list(
            Movie.select(Movie.id).where(
                cls._normalized_movie_number_expression().in_(normalized_movie_numbers)
            )
        )
        matched_movie_ids = [movie.id for movie in matched_movies]
        if not matched_movie_ids:
            return MovieCollectionMarkResponse(
                requested_count=requested_count,
                updated_count=0,
            )

        target_is_collection = collection_type == MovieCollectionMarkType.COLLECTION
        # 手工批量标记后写入 override 标识，后续自动规则同步不再覆盖这些影片。
        (
            Movie.update(
                is_collection=target_is_collection,
                is_collection_overridden=True,
            )
            .where(Movie.id.in_(matched_movie_ids))
            .execute()
        )
        return MovieCollectionMarkResponse(
            requested_count=requested_count,
            updated_count=len(matched_movie_ids),
        )

    @classmethod
    def get_movie_reviews(
        cls,
        movie_number: str,
        page: int = 1,
        page_size: int = 20,
        sort: MovieReviewSort = MovieReviewSort.RECENTLY,
    ) -> List[JavdbMovieReviewResource]:
        movie = cls._require_movie(movie_number)
        sort_value = sort.value if isinstance(sort, MovieReviewSort) else str(sort)
        try:
            return cls._build_javdb_provider().get_movie_reviews_by_javdb_id(
                movie.javdb_id,
                page=page,
                limit=page_size,
                sort_by=sort_value,
            )
        except MetadataNotFoundError as exc:
            # 本地影片已存在但远端评论接口返回 not found 时，仍统一映射为影片不存在。
            raise ApiError(
                404,
                "movie_not_found",
                "影片不存在",
                {"movie_number": movie_number, "javdb_id": movie.javdb_id},
            ) from exc
        except MetadataRequestError as exc:
            # 保留 javdb_id 与原始错误信息，方便定位远端请求失败原因。
            raise ApiError(
                502,
                "movie_review_fetch_failed",
                "影片评论拉取失败",
                {
                    "movie_number": movie_number,
                    "javdb_id": movie.javdb_id,
                    "detail": str(exc),
                },
            ) from exc

    @classmethod
    def refresh_movie_metadata(cls, movie_number: str) -> MovieDetailResource:
        movie, normalized_movie_number = cls._require_movie_by_normalized_number(movie_number)
        detail = cls._fetch_remote_movie_metadata(
            movie=movie,
            normalized_movie_number=normalized_movie_number,
        )
        local_normalized_movie_number = cls._validate_remote_movie_metadata_number(
            movie=movie,
            detail=detail,
        )
        cls._validate_remote_movie_metadata_javdb_id(
            movie=movie,
            detail=detail,
            normalized_movie_number=local_normalized_movie_number,
        )

        try:
            refreshed_movie = cls._build_catalog_import_service().refresh_movie_metadata_strict(movie, detail)
        except ImageDownloadError as exc:
            cls._raise_movie_metadata_refresh_failed(
                movie_number=movie.movie_number,
                normalized_movie_number=local_normalized_movie_number,
                exc=exc,
            )
        except Exception as exc:
            cls._raise_movie_metadata_refresh_failed(
                movie_number=movie.movie_number,
                normalized_movie_number=local_normalized_movie_number,
                exc=exc,
                log_message="Movie metadata refresh failed movie_number={} normalized={} detail={}",
            )

        return cls.get_movie_detail(refreshed_movie.movie_number)

    @classmethod
    def translate_movie_desc(cls, movie_number: str) -> MovieDetailResource:
        movie, _ = cls._require_movie_by_normalized_number(movie_number)
        translation_service = cls._build_movie_desc_translation_service()
        try:
            ActivityService.run_task(
                task_key=MovieDescTranslationService.TASK_KEY,
                trigger_type="manual",
                func=lambda _reporter: translation_service.translate_movie(movie),
            )
        except MovieDescTranslationClientError as exc:
            raise ApiError(
                exc.status_code,
                exc.error_code,
                exc.message,
                {"movie_number": movie.movie_number, "movie_id": movie.id},
            ) from exc
        except MovieDescTranslationTaskAbortError as exc:
            raise ApiError(
                exc.status_code or 500,
                exc.error_code or "movie_desc_translation_failed",
                exc.message,
                {"movie_number": movie.movie_number, "movie_id": movie.id, "detail": exc.message},
            ) from exc
        return cls.get_movie_detail(movie.movie_number)

    @classmethod
    def sync_movie_interactions(cls, movie_number: str) -> MovieDetailResource:
        movie, _ = cls._require_movie_by_normalized_number(movie_number)
        if not str(movie.javdb_id or "").strip():
            raise ApiError(
                422,
                "movie_javdb_id_missing",
                "影片缺少 JavDB ID",
                {"movie_number": movie.movie_number, "movie_id": movie.id},
            )

        interaction_service = cls._build_movie_interaction_sync_service()
        try:
            ActivityService.run_task(
                task_key=MovieInteractionSyncService.TASK_KEY,
                trigger_type="manual",
                func=lambda _reporter: interaction_service.sync_movie(movie),
            )
        except Exception as exc:
            logger.exception(
                "Movie interaction sync failed movie_number={} detail={}",
                movie.movie_number,
                exc,
            )
            raise ApiError(
                502,
                "movie_interaction_sync_failed",
                "影片互动数同步失败",
                {"movie_number": movie.movie_number, "movie_id": movie.id, "detail": str(exc)},
            ) from exc
        return cls.get_movie_detail(movie.movie_number)

    @classmethod
    def recompute_movie_heat(cls, movie_number: str) -> MovieDetailResource:
        movie, _ = cls._require_movie_by_normalized_number(movie_number)
        try:
            ActivityService.run_task(
                task_key="movie_heat_update",
                trigger_type="manual",
                func=lambda _reporter: {
                    # 单影片热度重算沿用现有公式，并把结果写入活动中心汇总。
                    "movie_id": movie.id,
                    "movie_number": movie.movie_number,
                    "updated_count": MovieHeatService.update_single_movie_heat(movie.id),
                    "formula_version": MovieHeatService.FORMULA_VERSION,
                },
            )
        except Exception as exc:
            logger.exception(
                "Movie heat recompute failed movie_number={} detail={}",
                movie.movie_number,
                exc,
            )
            raise ApiError(
                500,
                "movie_heat_recompute_failed",
                "影片热度重算失败",
                {"movie_number": movie.movie_number, "movie_id": movie.id, "detail": str(exc)},
            ) from exc
        return cls.get_movie_detail(movie.movie_number)

    @classmethod
    def stream_missav_thumbnails(
        cls,
        movie_number: str,
        *,
        refresh: bool = False,
    ) -> Iterator[tuple[str, dict]]:
        normalized_movie_number = normalize_movie_number(movie_number)
        yield "search_started", {
            "movie_number": normalized_movie_number or movie_number,
            "refresh": refresh,
        }
        if not normalized_movie_number:
            yield "completed", {
                "success": False,
                "reason": "missav_thumbnail_not_found",
                "detail": "movie number is invalid",
            }
            return

        progress_queue: Queue[tuple[str, dict] | None] = Queue()
        worker_result: dict[str, object] = {}
        worker_error: dict[str, Exception] = {}

        def _handle_progress(event: str, payload: dict) -> None:
            progress_queue.put((event, payload))

        def _run_fetch() -> None:
            try:
                worker_result["resource"] = cls._build_missav_thumbnail_service().get_movie_thumbnails(
                    normalized_movie_number,
                    refresh=refresh,
                    progress_callback=_handle_progress,
                )
            except Exception as exc:
                worker_error["error"] = exc
            finally:
                progress_queue.put(None)

        worker = Thread(target=_run_fetch, name="missav-thumbnail-stream", daemon=True)
        worker.start()

        while True:
            queued_event = progress_queue.get()
            if queued_event is None:
                break
            yield queued_event

        worker.join()
        error = worker_error.get("error")
        if isinstance(error, MissavThumbnailNotFoundError):
            yield "completed", {
                "success": False,
                "reason": "missav_thumbnail_not_found",
                "detail": str(error),
            }
            return
        if isinstance(error, MissavThumbnailRequestError):
            yield "completed", {
                "success": False,
                "reason": "missav_thumbnail_fetch_failed",
                "detail": str(error),
            }
            return
        if error is not None:
            logger.exception(
                "Missav thumbnail stream failed movie_number={} detail={}",
                normalized_movie_number,
                error,
            )
            yield "completed", {
                "success": False,
                "reason": "missav_thumbnail_fetch_failed",
                "detail": str(error),
            }
            return

        resource = worker_result["resource"]

        yield "completed", {
            "success": True,
            "result": resource.model_dump(),
        }

    @classmethod
    def set_subscription(cls, movie_number: str, subscribed: bool) -> None:
        movie = cls._require_movie(movie_number)
        was_subscribed = bool(movie.is_subscribed)
        movie.is_subscribed = subscribed
        if subscribed:
            if not was_subscribed or movie.subscribed_at is None:
                movie.subscribed_at = utc_now_for_db()
        else:
            movie.subscribed_at = None
        movie.save()

    @classmethod
    def unsubscribe_movie(cls, movie_number: str) -> None:
        movie = cls._require_movie(movie_number)
        media_items = cls._list_movie_media(movie)
        # 已有本地媒体时直接阻止取消订阅，避免把“停止追踪影片”和“删除本地资源”混成一个动作。
        if media_items:
            raise ApiError(
                409,
                "movie_subscription_has_media",
                "影片存在媒体文件，无法取消订阅",
                {
                    "movie_number": movie_number,
                    "media_count": len(media_items),
                },
            )

        movie.is_subscribed = False
        movie.subscribed_at = None
        movie.save()

    @classmethod
    def stream_search_and_upsert_movie_from_javdb(
        cls,
        movie_number: str,
    ) -> Iterator[tuple[str, dict]]:
        """按 SSE 事件顺序输出影片搜索和导入进度。"""
        normalized_movie_number = normalize_movie_number(movie_number)
        yield "search_started", {"movie_number": normalized_movie_number}

        if not normalized_movie_number:
            yield "completed", {"success": False, "reason": "movie_number_not_found", "movies": []}
            return

        try:
            detail = cls._build_javdb_provider().get_movie_by_number(normalized_movie_number)
        except MetadataNotFoundError:
            yield "completed", {"success": False, "reason": "movie_not_found", "movies": []}
            return
        except Exception as exc:
            logger.exception(
                "Javdb movie search failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            yield "completed", {"success": False, "reason": "internal_error", "movies": []}
            return

        # 先把搜索命中的原始远端信息回给前端，再开始实际落库。
        yield "movie_found", {
            "movies": [
                {
                    "javdb_id": detail.javdb_id,
                    "movie_number": detail.movie_number,
                    "title": detail.title,
                    "cover_image": detail.cover_image,
                }
            ],
            "total": 1,
        }
        yield "upsert_started", {"total": 1}

        created_count = 0
        already_exists_count = 0
        failed_count = 0
        failed_items: List[Dict[str, str]] = []
        imported_movies: List[MovieListItemResource] = []
        stats = {
            "total": 1,
            "created_count": created_count,
            "already_exists_count": already_exists_count,
            "failed_count": failed_count,
        }

        existed_before_upsert = Movie.get_or_none(
            (Movie.movie_number == detail.movie_number) | (Movie.javdb_id == detail.javdb_id)
        ) is not None
        try:
            # upsert 成功后重新走列表查询，确保响应里带上封面和 can_play 等派生字段。
            movie = cls._build_catalog_import_service().upsert_movie_from_javdb_detail(detail)
            movie_with_cover = cls._movie_list_query().where(Movie.id == movie.id).get_or_none() or movie
            imported_movies.append(MovieListItemResource.from_attributes_model(movie_with_cover))
            if existed_before_upsert:
                already_exists_count += 1
            else:
                created_count += 1
        except ImageDownloadError as exc:
            failed_count += 1
            logger.warning(
                "Javdb movie image download failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            failed_items.append(
                {
                    "movie_number": normalized_movie_number,
                    "reason": "image_download_failed",
                    "detail": str(exc),
                }
            )
        except Exception as exc:
            failed_count += 1
            logger.exception(
                "Javdb movie upsert failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            failed_items.append(
                {
                    "movie_number": normalized_movie_number,
                    "reason": "upsert_failed",
                    "detail": str(exc),
                }
            )

        stats["created_count"] = created_count
        stats["already_exists_count"] = already_exists_count
        stats["failed_count"] = failed_count
        yield "upsert_finished", stats

        if imported_movies:
            yield "completed", {
                "success": True,
                "movies": [movie_item.model_dump(exclude={"can_play"}) for movie_item in imported_movies],
                "failed_items": failed_items,
                "stats": stats,
            }
            return

        yield "completed", {
            "success": False,
            "reason": "internal_error",
            "movies": [],
            "failed_items": failed_items,
            "stats": stats,
        }

    @classmethod
    def stream_import_series_movies_from_javdb(
        cls,
        series_id: int,
    ) -> Iterator[tuple[str, dict]]:
        """按 SSE 事件顺序输出系列影片抓取和导入进度。"""
        yield "search_started", {"series_id": series_id}

        local_series = MovieSeries.get_or_none(MovieSeries.id == series_id)
        if local_series is None:
            yield "completed", {"success": False, "reason": "local_series_not_found", "movies": []}
            return

        series_name = local_series.name.strip()
        yield "series_found", {"series_id": local_series.id, "series_name": series_name}

        provider = cls._build_javdb_provider()
        try:
            series_candidates = provider.search_series(series_name)
        except Exception as exc:
            logger.exception("Javdb series search failed series_id={} series_name={} detail={}", series_id, series_name, exc)
            yield "completed", {"success": False, "reason": "metadata_fetch_failed", "movies": []}
            return

        # 只接受精确同名系列，避免把相似系列误导入本地系列。
        javdb_series = next(
            (candidate for candidate in series_candidates if candidate.name.strip() == series_name),
            None,
        )
        if javdb_series is None:
            yield "completed", {"success": False, "reason": "javdb_series_not_found", "movies": []}
            return

        yield "javdb_series_found", {
            "javdb_id": javdb_series.javdb_id,
            "javdb_type": javdb_series.javdb_type,
            "name": javdb_series.name,
            "videos_count": javdb_series.videos_count,
        }

        try:
            remote_movies = provider.get_series_movies(
                javdb_series.javdb_id,
                series_type=javdb_series.javdb_type,
            )
        except Exception as exc:
            logger.exception(
                "Javdb series movies fetch failed series_id={} javdb_series_id={} detail={}",
                series_id,
                javdb_series.javdb_id,
                exc,
            )
            yield "completed", {"success": False, "reason": "metadata_fetch_failed", "movies": []}
            return

        deduplicated_movies = []
        seen_movie_keys: set[str] = set()
        for movie_item in remote_movies:
            movie_key = movie_item.javdb_id or movie_item.movie_number
            if movie_key in seen_movie_keys:
                continue
            seen_movie_keys.add(movie_key)
            deduplicated_movies.append(movie_item)

        total = len(deduplicated_movies)
        if total == 0:
            yield "completed", {"success": False, "reason": "javdb_series_movies_not_found", "movies": []}
            return

        yield "movie_found", {
            "movies": [
                {
                    "javdb_id": movie_item.javdb_id,
                    "movie_number": movie_item.movie_number,
                    "title": movie_item.title,
                    "cover_image": movie_item.cover_image,
                }
                for movie_item in deduplicated_movies
            ],
            "total": total,
        }
        yield "upsert_started", {"total": total}

        created_count = 0
        already_exists_count = 0
        failed_count = 0
        skipped_items: List[Dict[str, str]] = []
        failed_items: List[Dict[str, str]] = []
        imported_movies: List[MovieListItemResource] = []
        import_service = cls._build_catalog_import_service()

        for index, movie_item in enumerate(deduplicated_movies, start=1):
            existing_movie = Movie.get_or_none(
                (Movie.javdb_id == movie_item.javdb_id) | (Movie.movie_number == movie_item.movie_number)
            )
            if existing_movie is not None:
                already_exists_count += 1
                skipped_item = {
                    "javdb_id": movie_item.javdb_id,
                    "movie_number": movie_item.movie_number,
                    "reason": "already_exists",
                }
                skipped_items.append(skipped_item)
                yield "movie_skipped", {**skipped_item, "index": index, "total": total}
                continue

            yield "movie_upsert_started", {
                "javdb_id": movie_item.javdb_id,
                "movie_number": movie_item.movie_number,
                "index": index,
                "total": total,
            }
            try:
                # 列表项信息不完整，入库前必须再拉详情复用统一导入链路。
                detail = provider.get_movie_by_javdb_id(movie_item.javdb_id)
                movie = import_service.upsert_movie_from_javdb_detail(detail)
                movie_with_cover = cls._movie_list_query().where(Movie.id == movie.id).get_or_none() or movie
                imported_movies.append(MovieListItemResource.from_attributes_model(movie_with_cover))
                created_count += 1
                yield "movie_upsert_finished", {
                    "javdb_id": detail.javdb_id,
                    "movie_number": detail.movie_number,
                    "index": index,
                    "total": total,
                }
            except ImageDownloadError as exc:
                failed_count += 1
                logger.warning(
                    "Javdb series movie image download failed series_id={} javdb_id={} detail={}",
                    series_id,
                    movie_item.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": movie_item.javdb_id,
                        "movie_number": movie_item.movie_number,
                        "reason": "image_download_failed",
                        "detail": str(exc),
                    }
                )
            except MetadataRequestError as exc:
                failed_count += 1
                logger.warning(
                    "Javdb series movie metadata fetch failed series_id={} javdb_id={} detail={}",
                    series_id,
                    movie_item.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": movie_item.javdb_id,
                        "movie_number": movie_item.movie_number,
                        "reason": "metadata_fetch_failed",
                        "detail": str(exc),
                    }
                )
            except Exception as exc:
                failed_count += 1
                logger.exception(
                    "Javdb series movie upsert failed series_id={} javdb_id={} detail={}",
                    series_id,
                    movie_item.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": movie_item.javdb_id,
                        "movie_number": movie_item.movie_number,
                        "reason": "upsert_failed",
                        "detail": str(exc),
                    }
                )

        stats = {
            "total": total,
            "created_count": created_count,
            "already_exists_count": already_exists_count,
            "failed_count": failed_count,
        }
        yield "upsert_finished", stats

        if imported_movies or skipped_items:
            yield "completed", {
                "success": True,
                "movies": [movie_item.model_dump(exclude={"can_play"}) for movie_item in imported_movies],
                "skipped_items": skipped_items,
                "failed_items": failed_items,
                "stats": stats,
            }
            return

        yield "completed", {
            "success": False,
            "reason": "internal_error",
            "movies": [],
            "skipped_items": skipped_items,
            "failed_items": failed_items,
            "stats": stats,
        }
