"""播放列表 service。

负责自定义播放列表和系统播放列表的增删改查，以及影片和播放列表之间的关系维护。
阅读入口建议从 ``list_playlists``、``list_playlist_movies``、``touch_recently_played`` 开始。
"""

from datetime import datetime

from peewee import Case, fn

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    build_ordered_expressions,
    count_by_owner,
    playable_exists_expression,
    require_by_id,
    require_record,
    resolve_sort_expression,
    with_movie_card_relations,
)
from src.i18n.playlist_names import system_playlist_reserved_names
from src.model import (
    PLAYLIST_KIND_CUSTOM,
    PLAYLIST_KIND_FAVORITES,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    PLAYLIST_KIND_SMART,
    RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    RECENTLY_PLAYED_PLAYLIST_NAME,
    SYSTEM_PLAYLIST_KINDS,
    Media,
    Movie,
    MovieRating,
    Playlist,
    PlaylistMovie,
    get_database,
)
from src.schema.catalog.movies import MovieListStatus, MovieWatchStatus, TagMatchMode
from src.schema.collections.playlists import (
    PlaylistCreateRequest,
    PlaylistMovieBatchAddResponse,
    PlaylistMovieBatchAddSkippedItem,
    PlaylistMovieListItemResource,
    PlaylistResolutionOption,
    PlaylistResource,
    PlaylistUpdateRequest,
    SmartPlaylistFilterResource,
)
from src.schema.common.pagination import PageResponse
from src.schema.common.playlists import PlaylistSummaryResource
from src.service.catalog.movie_list_media_service import attach_movie_list_media
from src.service.catalog.movie_resolution_service import (
    RESOLUTION_LEVELS,
    resolution_interval,
    resolution_level_expression,
)

# 系统列表内部展示次序：收藏在前，最近播放在后，自定义列表最后。
_SYSTEM_KIND_ORDER = (
    (PLAYLIST_KIND_FAVORITES, 0),
    (PLAYLIST_KIND_RECENTLY_PLAYED, 1),
)

# 允许空值、排序时统一垫后的字段（added_at/bitrate 走子查询无媒体场景由 COALESCE 兜底，不参与垫后）。
PLAYLIST_NULLABLE_SORT_FIELDS = {"release_date", "subscribed_at", "my_score"}


class PlaylistService:
    """聚合播放列表查询、名称校验和最近播放维护逻辑。"""

    SYSTEM_KINDS = set(SYSTEM_PLAYLIST_KINDS)
    # 成员可由用户增删的列表：自定义列表 + 系统收藏；最近播放由播放进度维护。
    MEMBER_MUTABLE_KINDS = {PLAYLIST_KIND_CUSTOM, PLAYLIST_KIND_FAVORITES}
    # 系统列表在所有语言下的名字都是保留名，避免界面出现两个同名列表。
    RESERVED_NAMES = system_playlist_reserved_names()

    @staticmethod
    def _playlist_system_order():
        """让系统播放列表固定排在普通列表之前，并给系统列表内部稳定次序。"""
        return Case(Playlist.kind, _SYSTEM_KIND_ORDER, len(_SYSTEM_KIND_ORDER))

    @staticmethod
    def _movie_playlist_system_order():
        """列出影片所属播放列表时同样优先展示系统列表。"""
        return Case(Playlist.kind, _SYSTEM_KIND_ORDER, len(_SYSTEM_KIND_ORDER))

    _playable_exists_expression = staticmethod(playable_exists_expression)

    @staticmethod
    def _latest_media_created_at_subquery():
        """查询影片最近一次本地媒体入库时间，供列表按入库时间排序/展示。"""
        return Media.select(fn.MAX(Media.created_at)).where(Media.movie == Movie.movie_number)

    @staticmethod
    def _max_bitrate_subquery():
        """查询影片全部有效媒体中的最高码率，供列表按码率排序。

        bit_rate 存在 ``video_info->'video'->>'bit_rate'``（TEXT 列需先 ``::json``），
        缺省/空串一律按 0 兜底参与排序，避免丢行。
        """
        bit_rate_text = fn.NULLIF(
            fn.json_extract_path_text(Media.video_info.cast("json"), "video", "bit_rate"),
            "",
        )
        return Media.select(fn.COALESCE(fn.MAX(bit_rate_text.cast("bigint")), 0)).where(
            Media.movie == Movie.movie_number,
            Media.valid == True,
        )

    @staticmethod
    def _rating_score_subquery():
        """查询影片个人评分总分，供播放列表统一排序使用。"""
        return MovieRating.select(MovieRating.total_score).where(
            MovieRating.movie == Movie.id
        )

    @classmethod
    def _build_playlist_sort(cls, sort: str | None):
        """解析 ``field:direction`` 播放列表排序，并补上稳定的次级排序；无效值抛 422。

        bitrate / added_at / my_score 用相关子查询（影片粒度），其余取 Movie 列。
        """
        def _subquery_order(field_name: str, direction: str) -> list:
            if field_name == "added_at":
                sort_field = cls._latest_media_created_at_subquery()
            elif field_name == "bitrate":
                sort_field = cls._max_bitrate_subquery()
            else:
                sort_field = cls._rating_score_subquery()
            return build_ordered_expressions(
                sort_field, direction,
                nullable=field_name in PLAYLIST_NULLABLE_SORT_FIELDS,
                tie_breaker=Movie.id,
            )

        return resolve_sort_expression(
            sort,
            {
                "heat": Movie.heat,
                "release_date": Movie.release_date,
                "added_at": Movie.id,
                "bitrate": Movie.id,
                "subscribed_at": Movie.subscribed_at,
                "my_score": Movie.id,
            },
            error_code="invalid_playlist_filter",
            nullable_fields=PLAYLIST_NULLABLE_SORT_FIELDS,
            tie_breaker=Movie.id,
            default=None,
            extra_sort_builders={
                "added_at": _subquery_order,
                "bitrate": _subquery_order,
                "my_score": _subquery_order,
            },
        )

    @staticmethod
    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ApiError(
                422,
                "validation_error",
                "Playlist name cannot be empty",
            )
        return normalized

    @staticmethod
    def _normalize_description(description: str | None) -> str:
        if description is None:
            return ""
        return description.strip()

    @classmethod
    def _ensure_name_available(cls, name: str, exclude_playlist_id: int | None = None) -> None:
        """校验播放列表名唯一；更新时允许排除当前列表自己。"""
        query = Playlist.select().where(Playlist.name == name)
        if exclude_playlist_id is not None:
            query = query.where(Playlist.id != exclude_playlist_id)
        if query.exists():
            raise ApiError(
                409,
                "playlist_name_conflict",
                "Playlist name already exists",
                {"name": name},
            )

    @classmethod
    def _ensure_name_not_reserved(cls, name: str) -> None:
        """系统保留名不允许被普通列表占用。"""
        if name in cls.RESERVED_NAMES:
            raise ApiError(
                409,
                "playlist_reserved_name",
                "Playlist name is reserved",
                {"name": name},
            )

    @staticmethod
    def _require_playlist(playlist_id: int) -> Playlist:
        return require_by_id(Playlist, playlist_id, "playlist", error_message="Playlist not found")

    @classmethod
    def _require_custom_playlist(cls, playlist_id: int) -> Playlist:
        """确保调用方操作的是自定义列表，而不是系统维护的列表。"""
        playlist = cls._require_playlist(playlist_id)
        if playlist.kind in cls.SYSTEM_KINDS:
            raise ApiError(
                409,
                "playlist_managed_by_system",
                "Playlist is managed by system",
                {"playlist_id": playlist.id},
            )
        return playlist

    @classmethod
    def _require_member_mutable_playlist(cls, playlist_id: int) -> Playlist:
        """允许自定义列表和收藏增删影片；最近播放和智能列表不接受手工增删。"""
        playlist = cls._require_playlist(playlist_id)
        if playlist.kind == PLAYLIST_KIND_SMART:
            raise ApiError(
                409,
                "smart_playlist_membership_read_only",
                "智能列表的影片由筛选条件自动维护",
                {"playlist_id": playlist.id},
            )
        if playlist.kind not in cls.MEMBER_MUTABLE_KINDS:
            raise ApiError(
                409,
                "playlist_managed_by_system",
                "Playlist is managed by system",
                {"playlist_id": playlist.id},
            )
        return playlist

    @staticmethod
    def _require_movie(movie_number: str) -> Movie:
        return require_record(
            Movie, Movie.movie_number == movie_number,
            error_code="movie_not_found",
            error_message="Movie not found",
            error_details={"movie_number": movie_number},
        )

    @staticmethod
    def _touch_playlist(playlist: Playlist, touched_at: datetime) -> None:
        playlist.updated_at = touched_at
        playlist.save(only=[Playlist.updated_at])

    @classmethod
    def _playlist_counts(cls, playlist_ids: list[int]) -> dict[int, int]:
        return count_by_owner(PlaylistMovie, PlaylistMovie.playlist, playlist_ids)

    @staticmethod
    def _smart_filter(playlist: Playlist) -> SmartPlaylistFilterResource:
        return SmartPlaylistFilterResource.model_validate(playlist.filter_json or {})

    @classmethod
    def _smart_movie_filters(cls, playlist: Playlist) -> dict:
        """把保存的筛选快照翻译成影片列表查询参数。

        空标签列表在影片列表语义里表示「不筛标签」，必须传 None；
        传空列表会被当成「命中 0 个标签」而清空整个列表。
        """
        saved = cls._smart_filter(playlist)
        return {
            "statuses": saved.statuses or None,
            "status_match": saved.status_match,
            "watch_statuses": saved.watch_statuses or None,
            "watch_match": saved.watch_match,
            "collection_type": saved.collection_type,
            "number_source": saved.number_source,
            "years": saved.years,
            "resolutions": saved.resolutions or None,
            "tag_ids": saved.tag_ids or None,
            "tag_match": saved.tag_match,
            "media_tag_ids": saved.media_tag_ids or None,
            "media_tag_match": saved.media_tag_match,
            "actor_age_min": saved.actor_age_min,
            "actor_age_max": saved.actor_age_max,
            "heat_min": saved.heat_min,
            "heat_max": saved.heat_max,
            "actor_ids": saved.actor_ids or None,
            "actor_match": saved.actor_match,
        }

    @classmethod
    def _smart_playlist_count(cls, playlist: Playlist) -> int:
        # 局部导入避免 movie_service → collections → 本模块的循环导入。
        from src.service.catalog.movie_service import MovieService

        return MovieService.count_movies(**cls._smart_movie_filters(playlist))

    @classmethod
    def _get_or_create_recently_played_playlist(cls) -> Playlist:
        """最近播放列表是系统单例，不允许外部创建多个实例。"""
        playlist = Playlist.get_or_none(Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED)
        if playlist is not None:
            return playlist
        return Playlist.create(
            kind=PLAYLIST_KIND_RECENTLY_PLAYED,
            name=RECENTLY_PLAYED_PLAYLIST_NAME,
            description=RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
        )

    @staticmethod
    def get_system_playlist_id(kind: str) -> int | None:
        playlist = Playlist.get_or_none(Playlist.kind == kind)
        return None if playlist is None else playlist.id

    @classmethod
    def list_playlists(cls, include_system: bool = True) -> list[PlaylistResource]:
        """列出播放列表，并补上每个列表的影片数量。"""
        query = Playlist.select().order_by(
            cls._playlist_system_order().asc(),
            Playlist.updated_at.desc(),
            Playlist.id.desc(),
        )
        if not include_system:
            query = query.where(Playlist.kind.not_in(cls.SYSTEM_KINDS))
        playlists = list(query)
        counts = cls._playlist_counts([playlist.id for playlist in playlists])
        resources: list[PlaylistResource] = []
        for playlist in playlists:
            movie_count = (
                cls._smart_playlist_count(playlist)
                if playlist.kind == PLAYLIST_KIND_SMART
                else counts.get(playlist.id, 0)
            )
            resources.append(
                PlaylistResource.from_playlist(playlist, movie_count=movie_count)
            )
        return resources

    @classmethod
    def create_playlist(cls, payload: PlaylistCreateRequest) -> PlaylistResource:
        is_smart = payload.kind == PLAYLIST_KIND_SMART
        if is_smart and payload.filter is None:
            raise ApiError(
                422,
                "smart_playlist_filter_required",
                "智能列表必须提供筛选条件",
            )
        if not is_smart and payload.filter is not None:
            raise ApiError(
                422,
                "smart_playlist_filter_not_supported",
                "普通播放列表不支持筛选条件",
            )
        if payload.filter is not None:
            # 分辨率档位是静态枚举，写入时就校验，避免坏值让列表读取整体 422。
            for resolution in payload.filter.resolutions:
                resolution_interval(resolution)
        name = cls._normalize_name(payload.name)
        description = cls._normalize_description(payload.description)
        cls._ensure_name_not_reserved(name)
        cls._ensure_name_available(name)
        playlist = Playlist.create(
            name=name,
            description=description,
            kind=payload.kind,
            filter_json=payload.filter.model_dump() if payload.filter else None,
        )
        movie_count = cls._smart_playlist_count(playlist) if is_smart else 0
        return PlaylistResource.from_playlist(playlist, movie_count=movie_count)

    @classmethod
    def get_playlist(cls, playlist_id: int) -> PlaylistResource:
        playlist = cls._require_playlist(playlist_id)
        movie_count = (
            cls._smart_playlist_count(playlist)
            if playlist.kind == PLAYLIST_KIND_SMART
            else cls._playlist_counts([playlist.id]).get(playlist.id, 0)
        )
        return PlaylistResource.from_playlist(
            playlist,
            movie_count=movie_count,
        )

    @classmethod
    def update_playlist(cls, playlist_id: int, payload: PlaylistUpdateRequest) -> PlaylistResource:
        playlist = cls._require_custom_playlist(playlist_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(
                422,
                "validation_error",
                "At least one field must be provided",
            )

        # 名称和描述都是局部可更新字段，未传的字段保持原值。
        if "name" in update_data:
            name = cls._normalize_name(update_data["name"])
            cls._ensure_name_not_reserved(name)
            if name != playlist.name:
                cls._ensure_name_available(name, exclude_playlist_id=playlist.id)
            playlist.name = name

        if "description" in update_data:
            playlist.description = cls._normalize_description(update_data["description"])

        if "filter" in update_data:
            if playlist.kind != PLAYLIST_KIND_SMART:
                raise ApiError(
                    422,
                    "smart_playlist_filter_not_supported",
                    "普通播放列表不支持筛选条件",
                )
            if payload.filter is None:
                raise ApiError(
                    422,
                    "smart_playlist_filter_required",
                    "智能列表必须提供筛选条件",
                )
            for resolution in payload.filter.resolutions:
                resolution_interval(resolution)
            playlist.filter_json = payload.filter.model_dump()

        playlist.updated_at = utc_now_for_db()
        playlist.save()
        movie_count = (
            cls._smart_playlist_count(playlist)
            if playlist.kind == PLAYLIST_KIND_SMART
            else cls._playlist_counts([playlist.id]).get(playlist.id, 0)
        )
        return PlaylistResource.from_playlist(
            playlist,
            movie_count=movie_count,
        )

    @classmethod
    def delete_playlist(cls, playlist_id: int) -> None:
        playlist = cls._require_custom_playlist(playlist_id)
        playlist.delete_instance(recursive=True)

    @staticmethod
    def _temporary_movie_filters(
        *,
        statuses: list[MovieListStatus] | None,
        status_match: TagMatchMode | None,
        watch_statuses: list[MovieWatchStatus] | None,
        watch_match: TagMatchMode | None,
        years: list[int] | None,
        resolutions: list[str] | None,
        tag_ids: list[int] | None,
        tag_match: TagMatchMode | None,
        media_tag_ids: list[int] | None,
        media_tag_match: TagMatchMode | None,
    ) -> dict:
        """把详情页临时筛选转成影片查询参数。

        None 表示沿用智能列表保存值；空列表/空标签表示临时清空该维度。
        标签与媒体标签的空列表要转成 None，避免被影片查询解释成「命中 0 个标签」。
        """
        filters: dict = {}
        if statuses is not None:
            filters["statuses"] = statuses
        if status_match is not None:
            filters["status_match"] = status_match
        if watch_statuses is not None:
            filters["watch_statuses"] = watch_statuses
        if watch_match is not None:
            filters["watch_match"] = watch_match
        if years is not None:
            filters["years"] = years
        if resolutions is not None:
            filters["resolutions"] = resolutions
        if tag_ids is not None:
            filters["tag_ids"] = tag_ids or None
        if tag_match is not None:
            filters["tag_match"] = tag_match
        if media_tag_ids is not None:
            filters["media_tag_ids"] = media_tag_ids or None
        if media_tag_match is not None:
            filters["media_tag_match"] = media_tag_match
        return filters

    @staticmethod
    def _has_active_movie_filters(filters: dict) -> bool:
        return any(
            bool(filters.get(key))
            for key in (
                "statuses",
                "watch_statuses",
                "years",
                "resolutions",
                "tag_ids",
                "media_tag_ids",
            )
        )

    @classmethod
    def list_playlist_movies(
        cls,
        playlist_id: int,
        page: int = 1,
        page_size: int = 20,
        *,
        sort: str | None = None,
        resolution: str | None = None,
        watch_status: MovieWatchStatus = MovieWatchStatus.ALL,
        statuses: list[MovieListStatus] | None = None,
        status_match: TagMatchMode | None = None,
        watch_statuses: list[MovieWatchStatus] | None = None,
        watch_match: TagMatchMode | None = None,
        years: list[int] | None = None,
        resolutions: list[str] | None = None,
        tag_ids: list[int] | None = None,
        tag_match: TagMatchMode | None = None,
        media_tag_ids: list[int] | None = None,
        media_tag_match: TagMatchMode | None = None,
    ) -> PageResponse[PlaylistMovieListItemResource]:
        """列出列表内影片，支持完整的详情页临时筛选与统一排序。

        智能列表以保存条件为基座，临时参数按维度覆盖；普通播放列表直接应用
        临时参数。不传 ``sort`` 时智能列表使用保存排序，普通列表按最近触达倒序。
        """
        playlist = cls._require_playlist(playlist_id)

        effective_resolutions = (
            resolutions
            if resolutions is not None
            else ([resolution] if resolution is not None else None)
        )
        effective_watch_statuses = (
            watch_statuses
            if watch_statuses is not None
            else (
                [watch_status]
                if watch_status is not None and watch_status != MovieWatchStatus.ALL
                else None
            )
        )
        for value in effective_resolutions or []:
            resolution_interval(value)

        temporary_filters = cls._temporary_movie_filters(
            statuses=statuses,
            status_match=status_match,
            watch_statuses=effective_watch_statuses,
            watch_match=watch_match,
            years=years,
            resolutions=effective_resolutions,
            tag_ids=tag_ids,
            tag_match=tag_match,
            media_tag_ids=media_tag_ids,
            media_tag_match=media_tag_match,
        )

        if playlist.kind == PLAYLIST_KIND_SMART:
            return cls._list_smart_playlist_movies(
                playlist,
                page,
                page_size,
                sort=sort,
                temporary_filters=temporary_filters,
            )
        start = max(page - 1, 0) * page_size
        temp_movie_ids = None
        if cls._has_active_movie_filters(temporary_filters):
            # 局部导入避免 movie_service → collections → 本模块的循环导入。
            from src.service.catalog.movie_service import MovieService

            temp_movie_ids = MovieService.filtered_movies_query(
                **temporary_filters
            ).select(Movie.id)
        total_query = (
            PlaylistMovie.select()
            .join(Movie, on=(PlaylistMovie.movie == Movie.id))
            .where(PlaylistMovie.playlist == playlist)
        )
        if temp_movie_ids is not None:
            total_query = total_query.where(Movie.id.in_(temp_movie_ids))
        total = total_query.count()
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        query, _thin_cover_alias = with_movie_card_relations(
            PlaylistMovie.select(PlaylistMovie, Movie, can_play_expression)
            .join(Movie, on=(PlaylistMovie.movie == Movie.id))
            .switch(Movie)
        )
        query = query.switch(PlaylistMovie).where(PlaylistMovie.playlist == playlist)
        if temp_movie_ids is not None:
            query = query.where(Movie.id.in_(temp_movie_ids))
        order_by = cls._build_playlist_sort(sort)
        if order_by is None:
            order_by = [PlaylistMovie.updated_at.desc(), PlaylistMovie.id.desc()]
        links = list(query.order_by(*order_by).offset(start).limit(page_size))
        attach_movie_list_media([link.movie for link in links])
        items: list[PlaylistMovieListItemResource] = []
        for link in links:
            # schema 读取的是 Movie 对象，所以把列表关系上的附加信息临时挂回 movie 实例。
            link.movie.playlist_item_updated_at = link.updated_at
            items.append(PlaylistMovieListItemResource.from_attributes_model(link.movie))
        return PageResponse[PlaylistMovieListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def _list_smart_playlist_movies(
        cls,
        playlist: Playlist,
        page: int,
        page_size: int,
        *,
        sort: str | None,
        temporary_filters: dict,
    ) -> PageResponse[PlaylistMovieListItemResource]:
        """智能列表没有成员表，列表内容由保存的筛选条件实时查询。

        请求里显式传入的临时筛选按维度覆盖保存值；未传时回落到保存值。
        排序同样是临时覆盖：未传时使用保存排序。
        """
        # 局部导入避免 movie_service → collections → 本模块的循环导入。
        from src.service.catalog.movie_service import MovieService

        saved = cls._smart_filter(playlist)
        filters = {**cls._smart_movie_filters(playlist), **temporary_filters}
        total = MovieService.count_movies(**filters)
        start = max(page - 1, 0) * page_size
        if sort:
            order_by = cls._build_playlist_sort(sort)
        else:
            try:
                order_by = cls._build_playlist_sort(saved.sort)
            except ApiError:
                # 保存的排序可能因评分维度被删等原因失效，退回默认排序保持列表可用。
                order_by = None
            if order_by is None:
                order_by = [Movie.movie_number.asc()]
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        movie_query, _thin_cover_alias = with_movie_card_relations(
            MovieService.filtered_movies_query(**filters).select_extend(
                can_play_expression
            )
        )
        movies = list(
            movie_query.order_by(*order_by).offset(start).limit(page_size)
        )
        attach_movie_list_media(movies)
        items: list[PlaylistMovieListItemResource] = []
        for movie in movies:
            # 智能列表没有关系行，用影片自身更新时间保持接口字段稳定。
            movie.playlist_item_updated_at = movie.updated_at
            items.append(PlaylistMovieListItemResource.from_attributes_model(movie))
        return PageResponse[PlaylistMovieListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def _bucket_for_level(level: int | None) -> str | None:
        """按序号把影片归入最高命中档位；无法解析的取 None 不计入。"""
        if level is None:
            return None
        for label, threshold in RESOLUTION_LEVELS:
            if level >= threshold:
                return label
        return None

    @classmethod
    def list_playlist_resolutions(cls, playlist_id: int) -> list[PlaylistResolutionOption]:
        """聚合播放列表内影片覆盖的分辨率档位（去重、按档位从高到低），供前端渲染筛选项。

        每部影片按其最高分辨率媒体归入唯一档位（8K/4K 互斥），与筛选语义一致。
        """
        playlist = cls._require_playlist(playlist_id)
        base = (
            Movie.select()
            .join(PlaylistMovie, on=(PlaylistMovie.movie == Movie.id))
            .where(PlaylistMovie.playlist == playlist)
        )
        max_level = fn.MAX(resolution_level_expression())
        # SQL 层只按影片聚合最高档位序号，分桶落在 Python，避免聚合函数进 GROUP BY。
        query = (
            base.select(Movie.id, max_level.alias("max_level"))
            .join(Media, on=(Media.movie == Movie.movie_number))
            .where(Media.valid == True, Media.resolution.regexp(r"^\d+x\d+$"))
            .group_by(Movie.id)
        )
        counts: dict[str, int] = {}
        for row in query:
            label = cls._bucket_for_level(row.max_level)
            if label is not None:
                counts[label] = counts.get(label, 0) + 1
        options: list[PlaylistResolutionOption] = []
        for label, _threshold in RESOLUTION_LEVELS:
            count = counts.get(label, 0)
            if count > 0:
                options.append(PlaylistResolutionOption(resolution=label, count=count))
        return options

    @classmethod
    def add_movie_to_playlist(cls, playlist_id: int, movie_number: str) -> None:
        playlist = cls._require_member_mutable_playlist(playlist_id)
        movie = cls._require_movie(movie_number)
        touched_at = utc_now_for_db()
        playlist_movie = PlaylistMovie.get_or_none(
            PlaylistMovie.playlist == playlist,
            PlaylistMovie.movie == movie,
        )
        if playlist_movie is None:
            PlaylistMovie.create(
                playlist=playlist,
                movie=movie,
                created_at=touched_at,
                updated_at=touched_at,
            )
        else:
            playlist_movie.updated_at = touched_at
            playlist_movie.save(only=[PlaylistMovie.updated_at])
        # 无论是新加还是重新加入，都把列表本身更新时间往前推，便于 UI 按最近活跃排序。
        cls._touch_playlist(playlist, touched_at)

    @classmethod
    def add_movies_to_playlist(
        cls,
        playlist_id: int,
        movie_numbers: list[str],
    ) -> PlaylistMovieBatchAddResponse:
        """批量加入影片；不存在的番号跳过，其余成员幂等写入。"""
        playlist = cls._require_member_mutable_playlist(playlist_id)
        movies = list(Movie.select().where(Movie.movie_number.in_(movie_numbers)))
        movies_by_number = {movie.movie_number: movie for movie in movies}
        found_numbers = [
            movie_number
            for movie_number in movie_numbers
            if movie_number in movies_by_number
        ]
        skipped = [
            PlaylistMovieBatchAddSkippedItem(
                movie_number=movie_number,
                reason="movie_not_found",
            )
            for movie_number in movie_numbers
            if movie_number not in movies_by_number
        ]
        if found_numbers:
            touched_at = utc_now_for_db()
            movie_ids = [movies_by_number[number].id for number in found_numbers]
            with get_database().atomic():
                existing_movie_ids = {
                    link.movie_id
                    for link in PlaylistMovie.select().where(
                        PlaylistMovie.playlist == playlist,
                        PlaylistMovie.movie.in_(movie_ids),
                    )
                }
                missing_movie_ids = [
                    movie_id
                    for movie_id in movie_ids
                    if movie_id not in existing_movie_ids
                ]
                if missing_movie_ids:
                    PlaylistMovie.insert_many(
                        [
                            {
                                "playlist": playlist.id,
                                "movie": movie_id,
                                "created_at": touched_at,
                                "updated_at": touched_at,
                            }
                            for movie_id in missing_movie_ids
                        ]
                    ).on_conflict_ignore().execute()
                PlaylistMovie.update(updated_at=touched_at).where(
                    PlaylistMovie.playlist == playlist,
                    PlaylistMovie.movie.in_(movie_ids),
                ).execute()
                cls._touch_playlist(playlist, touched_at)
        return PlaylistMovieBatchAddResponse(
            requested_count=len(movie_numbers),
            updated_count=len(found_numbers),
            skipped_count=len(skipped),
            skipped=skipped,
        )

    @classmethod
    def remove_movie_from_playlist(cls, playlist_id: int, movie_number: str) -> None:
        playlist = cls._require_member_mutable_playlist(playlist_id)
        movie = Movie.get_or_none(Movie.movie_number == movie_number)
        if movie is None:
            return
        deleted_count = (
            PlaylistMovie.delete()
            .where(
                PlaylistMovie.playlist == playlist,
                PlaylistMovie.movie == movie,
            )
            .execute()
        )
        if deleted_count:
            cls._touch_playlist(playlist, utc_now_for_db())

    @classmethod
    def touch_recently_played(cls, movie: Movie) -> None:
        """把影片写入系统最近播放列表，并刷新排序时间。"""
        playlist = cls._get_or_create_recently_played_playlist()
        touched_at = utc_now_for_db()
        playlist_movie = PlaylistMovie.get_or_none(
            PlaylistMovie.playlist == playlist,
            PlaylistMovie.movie == movie,
        )
        if playlist_movie is None:
            PlaylistMovie.create(
                playlist=playlist,
                movie=movie,
                created_at=touched_at,
                updated_at=touched_at,
            )
        else:
            playlist_movie.updated_at = touched_at
            playlist_movie.save(only=[PlaylistMovie.updated_at])
        cls._touch_playlist(playlist, touched_at)

    @classmethod
    def list_movie_playlists(cls, movie: Movie) -> list[PlaylistSummaryResource]:
        playlists = list(
            Playlist.select()
            .join(PlaylistMovie)
            .where(PlaylistMovie.movie == movie)
            .order_by(
                cls._movie_playlist_system_order().asc(),
                Playlist.name.asc(),
                Playlist.id.asc(),
            )
        )
        return [PlaylistSummaryResource.from_playlist(playlist) for playlist in playlists]
