"""影片目录 service。

负责影片列表、详情组装、订阅状态维护与合集标记这类"以本地记录为主"的查询和小型状态流转。
远端元数据刷新与 JavDB 流式导入见 ``movie_metadata_refresh_service``；
翻译/互动/热度/Missav 截图等异步任务见 ``movie_task_service``。
"""

from datetime import datetime
from typing import Dict, List, Optional, Sequence

from peewee import JOIN, Ordering, fn

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
from src.metadata._providers.javdb import JavdbProvider
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
from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import (
    MOVIE_LIST_SORT_FIELDS,
    MovieCollectionMarkResponse,
    MovieCollectionMarkType,
    MovieCollectionStatusResource,
    MovieCollectionType,
    MovieMediaPointResource,
    MovieMediaProgressResource,
    MovieMediaResource,
    MovieDetailResource,
    MovieListItemResource,
    MovieListStatus,
    MovieNumberSource,
    MovieSpecialTagFilter,
    TagMatchMode,
    MovieNumberParseResponse,
    MovieReviewSort,
    TagResource,
)
from src.schema.common.pagination import PageResponse
from src.metadata._providers.models import JavdbMovieReviewResource
from src.service.collections import PlaylistService


class MovieService:
    """聚合 Movie 相关查询、详情拼装和本地状态流转。"""

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
        tag_match: TagMatchMode = TagMatchMode.OR,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        special_tag: MovieSpecialTagFilter | None = None,
        series_id: int | None = None,
        director_name: str | None = None,
        maker_name: str | None = None,
        number_source: MovieNumberSource = MovieNumberSource.ALL,
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
            if tag_match == TagMatchMode.AND:
                # AND：影片须同时关联全部 tag，按影片分组后命中的去重标签数等于请求标签数。
                tagged_movie_ids = (
                    MovieTag.select(MovieTag.movie)
                    .where(MovieTag.tag.in_(tag_ids))
                    .group_by(MovieTag.movie)
                    .having(fn.COUNT(fn.DISTINCT(MovieTag.tag)) == len(tag_ids))
                )
            else:
                # OR：命中任意一个 tag 即可。
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
        if number_source == MovieNumberSource.FC2:
            # 番号统一规范化为大写存储，FC2 影片以 "FC2" 前缀开头。
            filtered_query = filtered_query.where(Movie.movie_number.startswith("FC2"))
        elif number_source == MovieNumberSource.REGULAR:
            filtered_query = filtered_query.where(~(Movie.movie_number.startswith("FC2")))
        return filtered_query

    @staticmethod
    def _latest_media_created_at_subquery():
        """查询影片最近一次本地媒体入库时间，供可播放列表按媒体入库排序。"""
        return Media.select(fn.MAX(Media.created_at)).where(Media.movie == Movie.movie_number)

    @classmethod
    def _build_movie_list_sort(cls, sort: Optional[str], status: MovieListStatus = MovieListStatus.ALL) -> Sequence:
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

        if field_name == "added_at" and status == MovieListStatus.PLAYABLE:
            sort_field = cls._latest_media_created_at_subquery()
            ordered_field = Ordering(sort_field, direction.upper())
        else:
            sort_field = cls.MOVIE_LIST_SORT_FIELD_MAP[field_name]
            ordered_field = sort_field.asc() if direction == "asc" else sort_field.desc()
        tie_breaker = Movie.id.asc() if direction == "asc" else Movie.id.desc()
        if field_name in cls.MOVIE_LIST_NULLABLE_SORT_FIELDS:
            # 允许空值的字段统一放到后面，避免不同数据库里空值排序行为不一致。
            return [sort_field.is_null(), ordered_field, tie_breaker]
        return [ordered_field, tie_breaker]

    @classmethod
    def movie_list_query(
        cls,
        actor_id: Optional[int] = None,
        tag_ids: list[int] | None = None,
        tag_match: TagMatchMode = TagMatchMode.OR,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        special_tag: MovieSpecialTagFilter | None = None,
        sort: Optional[str] = None,
        series_id: int | None = None,
        director_name: str | None = None,
        maker_name: str | None = None,
        number_source: MovieNumberSource = MovieNumberSource.ALL,
    ):
        """列表查询统一在这里补齐封面图和 ``can_play`` 计算列。"""
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        is_4k_expression = cls._special_tag_exists_expression("4K").alias("is_4k")
        query, _thin_cover_alias = with_movie_card_relations(
            cls._filtered_movies(
                actor_id,
                tag_ids,
                tag_match,
                year,
                status,
                collection_type,
                special_tag,
                series_id,
                director_name,
                maker_name,
                number_source,
            ).select(Movie, can_play_expression, is_4k_expression)
        )
        return query.order_by(*cls._build_movie_list_sort(sort, status))

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

    @staticmethod
    def _require_movie(movie_number: str) -> Movie:
        return require_record(
            Movie, Movie.movie_number == movie_number,
            error_code="movie_not_found",
            error_message="影片不存在",
            error_details={"movie_number": movie_number},
        )

    @classmethod
    def require_movie_by_normalized_number(cls, movie_number: str) -> tuple[Movie, str]:
        # 跨服务共享：MovieMetadataRefreshService / MovieTaskService 都会用它把入参番号
        # 归一化后定位到本地 Movie，作为公开 API 稳定住调用契约。
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
        tag_match: TagMatchMode = TagMatchMode.OR,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        special_tag: MovieSpecialTagFilter | None = None,
        number_source: MovieNumberSource = MovieNumberSource.ALL,
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
            tag_match,
            year,
            status,
            collection_type,
            special_tag,
            None,
            director_name,
            maker_name,
            number_source,
        ).count()
        movies = list(
            MovieService.movie_list_query(
                actor_id,
                tag_ids,
                tag_match,
                year,
                status,
                collection_type,
                special_tag,
                sort,
                None,
                director_name,
                maker_name,
                number_source,
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
            MovieService.movie_list_query(series_id=series_id, sort=sort)
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

    @classmethod
    def list_special_tag_movies(
        cls,
        special_tag: MovieSpecialTagFilter,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Movie], int]:
        """按特殊标签(VR/4K)实时过滤影片，按最近媒体入库时间倒序分页。

        供 VR/4K 虚拟系统播放列表复用：成员关系不落库，完全由 ``Media.special_tags`` 派生。
        返回的 Movie 实例额外携带 ``can_play`` / ``is_4k`` / ``playlist_item_updated_at`` 计算列。
        """
        start = max(page - 1, 0) * page_size
        # total 走 EXISTS 子查询并基于 Movie 去重，避免 join media 后按媒体行放大。
        total = cls._filtered_movies(special_tag=special_tag).count()
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        is_4k_expression = cls._special_tag_exists_expression("4K").alias("is_4k")
        # 用最近一次媒体入库时间作为列表内排序键与 playlist_item_updated_at 取值。
        latest_media_created_at = fn.MAX(Media.created_at)
        query, thin_cover_alias = with_movie_card_relations(
            Movie.select(
                Movie,
                can_play_expression,
                is_4k_expression,
                latest_media_created_at.alias("playlist_item_updated_at"),
            )
            .join(Media)
            .switch(Movie)
        )
        movies = list(
            query
            .where(cls._special_tag_exists_expression(special_tag.to_media_tag()))
            .group_by(Movie.id, Image.id, thin_cover_alias.id, MovieSeries.id)
            .order_by(latest_media_created_at.desc(), Movie.id.desc())
            .offset(start)
            .limit(page_size)
        )
        return movies, total

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
            cls.movie_list_query().where(
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
