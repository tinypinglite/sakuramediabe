"""影片目录 service。

负责影片列表、详情组装、订阅状态维护与合集标记这类"以本地记录为主"的查询和小型状态流转。
远端元数据刷新与 JavDB 流式导入见 ``movie_metadata_refresh_service``；
翻译/互动/热度等异步任务见 ``movie_task_service``。
"""

from collections.abc import Sequence
from datetime import datetime

from peewee import JOIN, fn

from src.api.exception.errors import ApiError
from src.common import (
    build_signed_media_url,
    build_signed_merged_media_url,
    parse_movie_number_from_text,
)
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    build_ordered_expressions,
    find_movie_by_number,
    playable_exists_expression,
    require_record,
    resolve_sort_expression,
    with_movie_card_relations,
)
from src.metadata._providers.models import JavdbMovieReviewResource
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import (
    Actor,
    Image,
    Media,
    MediaLibrary,
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
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import (
    MovieBlacklistBatchRequest,
    MovieCollectionMarkResponse,
    MovieCollectionMarkType,
    MovieCollectionStatusResource,
    MovieCollectionType,
    MovieDetailResource,
    MovieListItemResource,
    MovieListStatus,
    MovieMediaPointResource,
    MovieMediaProgressResource,
    MovieMediaResource,
    MovieMergedPlaybackResource,
    MovieMergePlaybackCandidateResource,
    MovieNumberParseResponse,
    MovieNumberSource,
    MovieReviewSort,
    MovieSubscriptionBatchResponse,
    MovieSubscriptionSkippedItem,
    TagMatchMode,
    TagResource,
)
from src.schema.common.pagination import PageResponse
from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway
from src.service.collections import PlaylistService


class MovieService:
    """聚合 Movie 相关查询、详情拼装和本地状态流转。"""

    # 批量订阅/取消订阅的跳过原因，前端按 movie_number 标注本次未处理的选择项。
    SUBSCRIPTION_SKIP_MOVIE_NOT_FOUND = "movie_not_found"
    SUBSCRIPTION_SKIP_HAS_MEDIA = "has_media"
    SUBSCRIPTION_SKIP_BLACKLISTED = "blacklisted"

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

    @classmethod
    def _filtered_movies(
        cls,
        actor_id: int | None = None,
        tag_ids: list[int] | None = None,
        tag_match: TagMatchMode = TagMatchMode.OR,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        series_id: int | None = None,
        director_name: str | None = None,
        maker_name: str | None = None,
        number_source: MovieNumberSource = MovieNumberSource.ALL,
        heat_min: int | None = None,
        heat_max: int | None = None,
        blacklisted: bool = False,
    ):
        """构建影片列表的基础筛选链路，供列表和计数查询复用。"""
        if heat_min is not None and heat_max is not None and heat_min > heat_max:
            raise ApiError(
                422,
                "invalid_movie_filter",
                "heat_min 不能大于 heat_max",
                {"heat_min": heat_min, "heat_max": heat_max},
            )
        query = Movie.select().where(Movie.is_blacklisted == blacklisted)
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
        elif status == MovieListStatus.UNSUBSCRIBED:
            filtered_query = filtered_query.where(Movie.is_subscribed == False)
        elif status == MovieListStatus.PLAYABLE:
            filtered_query = filtered_query.where(cls._playable_exists_expression())

        if collection_type == MovieCollectionType.SINGLE:
            filtered_query = filtered_query.where(Movie.is_collection == False)
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
        if heat_min is not None:
            filtered_query = filtered_query.where(Movie.heat >= heat_min)
        if heat_max is not None:
            filtered_query = filtered_query.where(Movie.heat <= heat_max)
        return filtered_query

    @staticmethod
    def _latest_media_created_at_subquery():
        """查询影片最近一次本地媒体入库时间，供可播放列表按媒体入库排序。"""
        return Media.select(fn.MAX(Media.created_at)).where(Media.movie == Movie.movie_number)

    @classmethod
    def _build_movie_list_sort(cls, sort: str | None, status: MovieListStatus = MovieListStatus.ALL) -> Sequence:
        """解析 ``field:direction`` 排序表达式，并补上稳定的次级排序。"""
        def _added_at_order(field_name: str, direction: str) -> list:
            # 可播放态按最近媒体入库时间排序：以媒体粒度子查询为排序列。
            return build_ordered_expressions(
                cls._latest_media_created_at_subquery(),
                direction,
                tie_breaker=Movie.id,
            )

        return resolve_sort_expression(
            sort,
            cls.MOVIE_LIST_SORT_FIELD_MAP,
            error_code="invalid_movie_filter",
            nullable_fields=cls.MOVIE_LIST_NULLABLE_SORT_FIELDS,
            tie_breaker=Movie.id,
            default=[Movie.movie_number.asc()],
            extra_sort_builders=(
                {"added_at": _added_at_order} if status == MovieListStatus.PLAYABLE else None
            ),
        )

    @classmethod
    def movie_list_query(
        cls,
        actor_id: int | None = None,
        tag_ids: list[int] | None = None,
        tag_match: TagMatchMode = TagMatchMode.OR,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        sort: str | None = None,
        series_id: int | None = None,
        director_name: str | None = None,
        maker_name: str | None = None,
        number_source: MovieNumberSource = MovieNumberSource.ALL,
        heat_min: int | None = None,
        heat_max: int | None = None,
        blacklisted: bool = False,
    ):
        """列表查询统一在这里补齐封面图和 ``can_play`` 计算列。"""
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        query, _thin_cover_alias = with_movie_card_relations(
            cls._filtered_movies(
                actor_id=actor_id,
                tag_ids=tag_ids,
                tag_match=tag_match,
                year=year,
                status=status,
                collection_type=collection_type,
                series_id=series_id,
                director_name=director_name,
                maker_name=maker_name,
                number_source=number_source,
                heat_min=heat_min,
                heat_max=heat_max,
                blacklisted=blacklisted,
            ).select(Movie, can_play_expression)
        )
        return query.order_by(*cls._build_movie_list_sort(sort, status))

    @classmethod
    def _latest_movies_query(cls):
        """按最近导入媒体时间倒序列出影片，而不是按影片自身创建时间。"""
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        latest_media_created_at = fn.MAX(Media.created_at)
        query, thin_cover_alias = with_movie_card_relations(
            Movie.select(Movie, can_play_expression)
            .join(Media)
            .switch(Movie)
            .where(Movie.is_blacklisted == False)
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
        query, thin_cover_alias = with_movie_card_relations(
            Movie.select(Movie, can_play_expression)
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
        # 定位到本地 Movie。第二个返回值是库内规范形态（provider 原样），后续查 provider
        # 必须用它而不是用户输入——两侧形态一致才能精确回查。
        movie = find_movie_by_number(movie_number)
        if movie is None:
            raise ApiError(404, "movie_not_found", "影片不存在", {"movie_number": movie_number})
        return movie, movie.movie_number

    @staticmethod
    def _list_movie_media(movie: Movie) -> list[Media]:
        return list(
            Media.select(Media)
            .where(Media.movie == movie)
            .order_by(Media.id)
        )

    @staticmethod
    def _actors(movie: Movie) -> list[Actor]:
        return list(
            Actor.select(Actor, Image)
            .join(Image, JOIN.LEFT_OUTER, on=(Actor.profile_image == Image.id))
            .join(MovieActor, JOIN.INNER, on=(MovieActor.actor == Actor.id))
            .where(MovieActor.movie == movie)
            .order_by(Actor.id)
        )

    @staticmethod
    def _plot_images(movie: Movie) -> list[Image]:
        query = (
            MoviePlotImage.select(MoviePlotImage, Image)
            .join(Image)
            .where(MoviePlotImage.movie == movie)
            .order_by(MoviePlotImage.id)
        )
        return [link.image for link in query]

    @staticmethod
    def _media_items(movie: Movie) -> list[MovieMediaResource]:
        """把媒体、播放进度和打点信息折叠成详情页需要的资源结构。"""
        media_items = list(
            Media.select(Media, MediaLibrary)
            .join(MediaLibrary, JOIN.LEFT_OUTER)
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

        points_by_media_id: dict[int, list[MovieMediaPointResource]] = {}
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

        resources: list[MovieMediaResource] = []
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
            bundle = MEDIA_PROVIDER_REGISTRY.require(media.library.provider_key)
            media.play_url = build_signed_media_url(
                media.id, delivery=bundle.playback_deliveries[0]
            )
            media.provider_key = media.library.provider_key
            media.playback_deliveries = list(bundle.playback_deliveries)
            resources.append(MovieMediaResource.from_attributes_model(media))
        return resources

    @staticmethod
    def _merge_playback_groups(
        movie: Movie,
    ) -> list[tuple[MediaLibrary, list[Media], str]]:
        media_items = list(
            Media.select(Media, MediaLibrary)
            .join(MediaLibrary)
            .where(Media.movie == movie)
            .order_by(Media.id)
        )
        groups: dict[int, tuple[MediaLibrary, list[Media]]] = {}
        for media in media_items:
            library = media.library
            entry = groups.get(library.id)
            if entry is None:
                groups[library.id] = (library, [media])
            else:
                entry[1].append(media)

        playable_groups: list[tuple[MediaLibrary, list[Media], str]] = []
        for library_id in sorted(groups):
            library, medias = groups[library_id]
            # 合并播放表示同库的完整分段集合；任一段失效时不能悄悄跳过它。
            if len(medias) < 2 or any(not media.valid for media in medias):
                continue
            bundle = MEDIA_PROVIDER_REGISTRY.require(library.provider_key)
            playback_format = getattr(bundle, "merged_playback_format", None)
            if playback_format not in {"mp4", "hls"}:
                continue
            playable_groups.append((library, medias, playback_format))
        return playable_groups

    @classmethod
    def _merge_playback_candidates(
        cls, movie: Movie
    ) -> list[MovieMergePlaybackCandidateResource]:
        return [
            MovieMergePlaybackCandidateResource(
                library_id=library.id,
                library_name=library.name,
                provider_key=library.provider_key,
                segment_count=len(medias),
            )
            for library, medias, _playback_format in cls._merge_playback_groups(movie)
        ]

    @classmethod
    def get_merged_playback(
        cls, movie_number: str, library_id: int
    ) -> MovieMergedPlaybackResource:
        movie = cls._require_movie(movie_number)
        for library, medias, playback_format in cls._merge_playback_groups(movie):
            if library.id != library_id:
                continue
            resource_path = "stream.mp4" if playback_format == "mp4" else "index.m3u8"
            return MovieMergedPlaybackResource(
                play_url=build_signed_merged_media_url(
                    (media.id for media in medias), resource_path
                ),
            )
        raise ApiError(422, "merged_playback_unavailable", "该媒体库不支持合并播放")

    @staticmethod
    def get_movie_detail(movie_number: str) -> MovieDetailResource:
        """组装影片详情页所需的所有关联资源。"""
        query, _thin_cover_alias = with_movie_card_relations(
            Movie.select(Movie)
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
        movie.merge_playback_candidates = MovieService._merge_playback_candidates(movie)
        movie.playlists = PlaylistService.list_movie_playlists(movie)
        movie.can_play = any(media_item.valid for media_item in movie.media_items)
        return MovieDetailResource.from_attributes_model(movie)

    @staticmethod
    def list_movies(
        actor_id: int | None = None,
        tag_ids: list[int] | None = None,
        tag_match: TagMatchMode = TagMatchMode.OR,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        number_source: MovieNumberSource = MovieNumberSource.ALL,
        sort: str | None = None,
        director_name: str | None = None,
        maker_name: str | None = None,
        heat_min: int | None = None,
        heat_max: int | None = None,
        blacklisted: bool = False,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MovieListItemResource]:
        start = max(page - 1, 0) * page_size
        total = MovieService._filtered_movies(
            actor_id=actor_id,
            tag_ids=tag_ids,
            tag_match=tag_match,
            year=year,
            status=status,
            collection_type=collection_type,
            director_name=director_name,
            maker_name=maker_name,
            number_source=number_source,
            heat_min=heat_min,
            heat_max=heat_max,
            blacklisted=blacklisted,
        ).count()
        movies = list(
            MovieService.movie_list_query(
                actor_id=actor_id,
                tag_ids=tag_ids,
                tag_match=tag_match,
                year=year,
                status=status,
                collection_type=collection_type,
                sort=sort,
                director_name=director_name,
                maker_name=maker_name,
                number_source=number_source,
                heat_min=heat_min,
                heat_max=heat_max,
                blacklisted=blacklisted,
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
        sort: str | None = None,
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
        # 用户输入整体就是扫描范围，不需要路径截断，直接走文本识别。
        parsed_movie_number = parse_movie_number_from_text(query.strip())
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
    def search_local_movies(cls, movie_number: str) -> list[MovieListItemResource]:
        # 本地搜索只取最匹配的一条，职责是回答“库里有没有这个番号”。
        movie = find_movie_by_number(movie_number)
        if movie is None:
            return []
        movies = list(cls.movie_list_query().where(Movie.id == movie.id))
        return MovieListItemResource.from_items(movies)

    @classmethod
    def get_movie_collection_status(cls, movie_number: str) -> MovieCollectionStatusResource:
        # 与本地搜索保持同一套匹配（find_movie_by_number），确保不同输入格式能命中同一影片。
        movie = find_movie_by_number(movie_number)
        if movie is None:
            raise ApiError(404, "movie_not_found", "影片不存在", {"movie_number": movie_number})

        return MovieCollectionStatusResource(
            movie_number=movie.movie_number,
            is_collection=bool(movie.is_collection),
        )

    @classmethod
    def mark_movie_collection_type(
        cls,
        movie_numbers: list[str],
        collection_type: MovieCollectionMarkType,
    ) -> MovieCollectionMarkResponse:
        requested_count = len(movie_numbers)
        # 批量入参按大小写不敏感的精确形态去重匹配；不做 '_'/'-' 互换——互换在两种分隔符
        # 影片同时存在时会把操作扩散到另一部片上，批量场景宁可 miss 也不能错标。
        movie_number_keys: list[str] = []
        seen_numbers: set[str] = set()
        for movie_number in movie_numbers:
            key = (movie_number or "").strip().upper()
            if not key or key in seen_numbers:
                continue
            seen_numbers.add(key)
            movie_number_keys.append(key)

        if not movie_number_keys:
            return MovieCollectionMarkResponse(
                requested_count=requested_count,
                updated_count=0,
            )

        matched_movies = list(
            Movie.select(Movie.id).where(
                fn.UPPER(Movie.movie_number).in_(movie_number_keys)
            )
        )
        matched_movie_ids = [movie.id for movie in matched_movies]
        if not matched_movie_ids:
            return MovieCollectionMarkResponse(
                requested_count=requested_count,
                updated_count=0,
            )

        target_is_collection = collection_type == MovieCollectionMarkType.COLLECTION
        # 人工标记取得宿主 owner，后续自动规则尊重该字段；不增加单独 override 状态。
        updated_count = MovieOwnershipGateway.update_host_manual(
            matched_movie_ids,
            {"is_collection": target_is_collection},
        )
        return MovieCollectionMarkResponse(
            requested_count=requested_count,
            updated_count=updated_count,
        )

    @classmethod
    def get_movie_reviews(
        cls,
        movie_number: str,
        page: int = 1,
        page_size: int = 20,
        sort: MovieReviewSort = MovieReviewSort.RECENTLY,
    ) -> list[JavdbMovieReviewResource]:
        movie = cls._require_movie(movie_number)
        sort_value = sort.value if isinstance(sort, MovieReviewSort) else str(sort)
        try:
            return build_javdb_provider().get_movie_reviews_by_javdb_id(
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
        if subscribed and movie.is_blacklisted:
            raise ApiError(
                409,
                "movie_is_blacklisted",
                "影片已在黑名单中，请先解除黑名单",
                {"movie_number": movie.movie_number},
            )
        was_subscribed = bool(movie.is_subscribed)
        movie.is_subscribed = subscribed
        reset_search_state = False
        if subscribed:
            if not was_subscribed or movie.subscribed_at is None:
                movie.subscribed_at = utc_now_for_db()
                reset_search_state = True
        else:
            movie.subscribed_at = None
        if reset_search_state:
            cls._reset_subscription_search_state(movie)
        # 窄更新：受保护字段白名单开放后裸 save() 会被护栏拒绝，订阅状态与标题无关。
        movie.save(
            only=[
                Movie.is_subscribed,
                Movie.subscribed_at,
                *cls._subscription_search_state_fields(),
            ]
        )

    @staticmethod
    def _subscription_search_state_fields() -> tuple:
        return (
            Movie.subscription_search_state,
            Movie.subscription_search_attempt_count,
            Movie.subscription_search_retry_round,
            Movie.subscription_search_last_attempted_at,
            Movie.subscription_search_last_succeeded_at,
            Movie.subscription_search_next_retry_at,
            Movie.subscription_search_error_code,
            Movie.subscription_search_last_error,
            Movie.subscription_search_last_error_at,
        )

    @staticmethod
    def _reset_subscription_search_state(movie: Movie) -> None:
        movie.subscription_search_state = "pending"
        movie.subscription_search_attempt_count = 0
        movie.subscription_search_retry_round = (movie.subscription_search_retry_round or 0) + 1
        movie.subscription_search_last_attempted_at = None
        movie.subscription_search_last_succeeded_at = None
        movie.subscription_search_next_retry_at = None
        movie.subscription_search_error_code = None
        movie.subscription_search_last_error = None
        movie.subscription_search_last_error_at = None

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
        movie.save(only=[Movie.is_subscribed, Movie.subscribed_at])

    @staticmethod
    def _dedup_movie_number_keys(
        movie_numbers: list[str],
    ) -> tuple[list[str], dict[str, str]]:
        """批量入参按大小写不敏感的精确 key（strip+upper）去重。

        返回有序 key 列表和 key->原始展示编号 的映射。不做 '_'/'-' 互换——互换在两种分隔符
        影片同时存在时（一本道/加勒比同日番号）会把批量操作扩散到另一部片上，宁可 miss
        进 skipped 让用户看见，也不能错订/错退。
        """
        ordered_keys: list[str] = []
        display_by_key: dict[str, str] = {}
        for movie_number in movie_numbers:
            key = (movie_number or "").strip().upper()
            if not key or key in display_by_key:
                continue
            display_by_key[key] = movie_number
            ordered_keys.append(key)
        return ordered_keys, display_by_key

    @classmethod
    def set_blacklisted(
        cls,
        payload: MovieBlacklistBatchRequest,
        *,
        blacklisted: bool,
    ) -> None:
        ordered_keys, display_by_key = cls._dedup_movie_number_keys(payload.movie_numbers)
        movies = list(Movie.select().where(fn.UPPER(Movie.movie_number).in_(ordered_keys)))
        matched_keys = {movie.movie_number.strip().upper() for movie in movies}
        missing = [display_by_key[key] for key in ordered_keys if key not in matched_keys]
        if missing:
            raise ApiError(
                404,
                "movie_not_found",
                "影片不存在",
                {"movie_numbers": missing},
            )
        if blacklisted:
            subscribed = [movie.movie_number for movie in movies if movie.is_subscribed]
            if subscribed:
                raise ApiError(
                    409,
                    "movie_is_subscribed",
                    "已订阅影片不能加入黑名单，请先取消订阅",
                    {"movie_numbers": subscribed},
                )
        Movie.update(is_blacklisted=blacklisted).where(
            Movie.id.in_([movie.id for movie in movies])
        ).execute()

    @classmethod
    def batch_set_subscription(
        cls, movie_numbers: list[str]
    ) -> MovieSubscriptionBatchResponse:
        # 批量订阅：逐条判定、部分成功，未命中番号进 skipped，不整批回滚。
        requested_count = len(movie_numbers)
        ordered_keys, display_by_key = cls._dedup_movie_number_keys(movie_numbers)
        if not ordered_keys:
            return MovieSubscriptionBatchResponse(
                requested_count=requested_count, updated_count=0
            )

        matched_movies = list(
            Movie.select().where(
                fn.UPPER(Movie.movie_number).in_(ordered_keys)
            )
        )
        matched_keys = {movie.movie_number.strip().upper() for movie in matched_movies}
        skipped = [
            MovieSubscriptionSkippedItem(
                movie_number=display_by_key[key],
                reason=cls.SUBSCRIPTION_SKIP_MOVIE_NOT_FOUND,
            )
            for key in ordered_keys
            if key not in matched_keys
        ]

        blacklisted_movies = [movie for movie in matched_movies if movie.is_blacklisted]
        skipped.extend(
            MovieSubscriptionSkippedItem(
                movie_number=movie.movie_number,
                reason=cls.SUBSCRIPTION_SKIP_BLACKLISTED,
            )
            for movie in blacklisted_movies
        )

        for movie in matched_movies:
            if movie.is_blacklisted:
                continue
            # 与单条 set_subscription(True) 一致：仅在原本未订阅或订阅时间为空时写入当前时间。
            was_subscribed = bool(movie.is_subscribed)
            movie.is_subscribed = True
            if not was_subscribed or movie.subscribed_at is None:
                movie.subscribed_at = utc_now_for_db()
                cls._reset_subscription_search_state(movie)
            movie.save(
                only=[
                    Movie.is_subscribed,
                    Movie.subscribed_at,
                    *cls._subscription_search_state_fields(),
                ]
            )

        return MovieSubscriptionBatchResponse(
            requested_count=requested_count,
            updated_count=len(matched_movies),
            skipped_count=len(skipped),
            skipped=skipped,
        )

    @classmethod
    def batch_unsubscribe_movies(
        cls, movie_numbers: list[str]
    ) -> MovieSubscriptionBatchResponse:
        # 批量取消订阅：存在本地媒体的影片按部分成功语义跳过（has_media），不报错也不回滚。
        requested_count = len(movie_numbers)
        ordered_keys, display_by_key = cls._dedup_movie_number_keys(movie_numbers)
        if not ordered_keys:
            return MovieSubscriptionBatchResponse(
                requested_count=requested_count, updated_count=0
            )

        matched_movies = list(
            Movie.select().where(
                fn.UPPER(Movie.movie_number).in_(ordered_keys)
            )
        )
        matched_keys = {movie.movie_number.strip().upper() for movie in matched_movies}
        skipped = [
            MovieSubscriptionSkippedItem(
                movie_number=display_by_key[key],
                reason=cls.SUBSCRIPTION_SKIP_MOVIE_NOT_FOUND,
            )
            for key in ordered_keys
            if key not in matched_keys
        ]

        # 一次聚合查询拿到"有本地媒体"的影片番号集合，避免逐条 _list_movie_media 的 N+1。
        # 关键：Media.movie 外键 field=Movie.movie_number（列即 media.movie_number），
        # in_ 参数必须传番号字符串列表，不能传 movie.id 整数——曾在此把 movie.id 传入导致
        # 生成 WHERE movie_number IN (1,2,3) 恒不命中，has_media 判定完全失效。
        matched_numbers = [movie.movie_number for movie in matched_movies]
        numbers_with_media = {
            row[0]
            for row in Media.select(Media.movie)
            .where(Media.movie.in_(matched_numbers))
            .distinct()
            .tuples()
        }

        updated_count = 0
        for movie in matched_movies:
            if movie.movie_number in numbers_with_media:
                skipped.append(
                    MovieSubscriptionSkippedItem(
                        movie_number=movie.movie_number,
                        reason=cls.SUBSCRIPTION_SKIP_HAS_MEDIA,
                    )
                )
                continue
            movie.is_subscribed = False
            movie.subscribed_at = None
            movie.save(only=[Movie.is_subscribed, Movie.subscribed_at])
            updated_count += 1

        return MovieSubscriptionBatchResponse(
            requested_count=requested_count,
            updated_count=updated_count,
            skipped_count=len(skipped),
            skipped=skipped,
        )
