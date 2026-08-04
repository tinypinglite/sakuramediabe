"""播放列表 service。

负责自定义播放列表和系统播放列表的增删改查，以及影片和播放列表之间的关系维护。
阅读入口建议从 ``list_playlists``、``list_playlist_movies``、``touch_recently_played`` 开始。
"""

from datetime import datetime

from peewee import Case, Ordering, fn

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    count_by_owner,
    playable_exists_expression,
    require_by_id,
    with_movie_card_relations,
)
from src.model import (
    FOUR_K_PLAYLIST_NAME,
    PLAYLIST_KIND_4K,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    PLAYLIST_KIND_VR,
    RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    RECENTLY_PLAYED_PLAYLIST_NAME,
    SYSTEM_PLAYLIST_KINDS,
    VR_PLAYLIST_NAME,
    Media,
    Movie,
    Playlist,
    PlaylistMovie,
)
from src.schema.catalog.movies import MovieSpecialTagFilter
from src.schema.collections.playlists import (
    PlaylistCreateRequest,
    PlaylistMovieListItemResource,
    PlaylistResolutionOption,
    PlaylistResource,
    PlaylistUpdateRequest,
)
from src.schema.common.pagination import PageResponse
from src.schema.common.playlists import PlaylistSummaryResource

# 虚拟系统列表 kind -> 对应特殊标签过滤，成员关系按 Media.special_tags 实时派生。
_VIRTUAL_KIND_TO_SPECIAL_TAG = {
    PLAYLIST_KIND_VR: MovieSpecialTagFilter.VR,
    PLAYLIST_KIND_4K: MovieSpecialTagFilter.FOUR_K,
}

# 系统列表内部展示次序：最近播放、VR、4K 在前，自定义列表在后。
_SYSTEM_KIND_ORDER = (
    (PLAYLIST_KIND_RECENTLY_PLAYED, 0),
    (PLAYLIST_KIND_VR, 1),
    (PLAYLIST_KIND_4K, 2),
)

# 播放列表影片排序白名单：heat(热度)/bitrate(最高码率)/added_at(最近媒体入库)/release_date(发布时间)。
PLAYLIST_SORT_FIELDS = {"heat", "bitrate", "added_at", "release_date"}
# 允许空值、排序时统一垫后的字段（added_at/bitrate 走子查询无媒体场景由 COALESCE 兜底，不参与垫后）。
PLAYLIST_NULLABLE_SORT_FIELDS = {"release_date"}
# 分辨率筛选档位：归一化标签 -> 高度阈值（Media.resolution 为 "WxH" 字符串，取 height 判定）。
RESOLUTION_LEVELS = (
    ("8K", 4320),
    ("4K", 2160),
    ("2K", 1440),
    ("1080P", 1080),
    ("720P", 720),
    ("480P", 480),
    ("360P", 360),
)


class PlaylistService:
    """聚合播放列表查询、名称校验和最近播放维护逻辑。"""

    SYSTEM_KINDS = set(SYSTEM_PLAYLIST_KINDS)
    RESERVED_NAMES = {RECENTLY_PLAYED_PLAYLIST_NAME, VR_PLAYLIST_NAME, FOUR_K_PLAYLIST_NAME}
    VIRTUAL_KINDS = set(_VIRTUAL_KIND_TO_SPECIAL_TAG)

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
    def _resolution_height_expression():
        """解析 ``WxH`` 分辨率的 height 分量（probe 写入形态），供阈值比较与分桶复用。"""
        return fn.split_part(Media.resolution, "x", 2).cast("int")

    @classmethod
    def _resolution_interval(cls, resolution: str | None) -> tuple[int | None, int | None]:
        """解析分辨率筛选档位为 ``[threshold, upper)`` 高度区间；非法档位抛 422。

        档位互斥：4K 命中 ``[2160, 4320)``，8K 命中 ``[4320, ...)``，8K 影片不会误入 4K。
        """
        if resolution is None:
            return (None, None)
        normalized = resolution.strip().lower()
        for index, (label, threshold) in enumerate(RESOLUTION_LEVELS):
            if label.lower() == normalized:
                upper = RESOLUTION_LEVELS[index - 1][1] if index > 0 else None
                return (threshold, upper)
        raise ApiError(
            422,
            "invalid_playlist_filter",
            "Invalid resolution filter",
            {"resolution": resolution},
        )

    @classmethod
    def _resolution_exists_expression(cls, resolution: str):
        """构造「影片最高分辨率落在档位区间」的 EXISTS 子查询。

        对 media 分组后按 MAX(height) 归入精确档位，只匹配 ``WxH`` 形态（probe 写入），
        无法解析的脏值直接排除。
        """
        threshold, upper = cls._resolution_interval(resolution)
        height_expression = cls._resolution_height_expression()
        having_conditions = [fn.MAX(height_expression) >= threshold]
        if upper is not None:
            having_conditions.append(fn.MAX(height_expression) < upper)
        return fn.EXISTS(
            Media.select(fn.COUNT(Media.id))
            .where(
                Media.movie == Movie.movie_number,
                Media.valid == True,
                Media.resolution.regexp(r"^\d+x\d+$"),
            )
            .group_by(Media.movie)
            .having(*having_conditions)
        )

    @classmethod
    def _build_playlist_sort(cls, sort: str | None):
        """解析 ``field:direction`` 播放列表排序，并补上稳定的次级排序；无效值抛 422。

        bitrate / added_at 用相关子查询（影片粒度），heat / release_date 取 Movie 列。
        """
        if sort is None:
            return None
        normalized = sort.strip().lower()
        if not normalized:
            return None
        try:
            field_name, direction = normalized.split(":", 1)
        except ValueError:
            raise ApiError(
                422,
                "invalid_playlist_filter",
                "Invalid sort expression",
                {"sort": sort},
            )
        if field_name not in PLAYLIST_SORT_FIELDS or direction not in ("asc", "desc"):
            raise ApiError(
                422,
                "invalid_playlist_filter",
                "Invalid sort expression",
                {"sort": sort},
            )
        if field_name == "heat":
            sort_field = Movie.heat
            ordered_field = sort_field.asc() if direction == "asc" else sort_field.desc()
        elif field_name == "release_date":
            sort_field = Movie.release_date
            ordered_field = sort_field.asc() if direction == "asc" else sort_field.desc()
        elif field_name == "added_at":
            sort_field = cls._latest_media_created_at_subquery()
            ordered_field = Ordering(sort_field, direction.upper())
        else:  # bitrate
            sort_field = cls._max_bitrate_subquery()
            ordered_field = Ordering(sort_field, direction.upper())
        tie_breaker = Movie.id.asc() if direction == "asc" else Movie.id.desc()
        if field_name in PLAYLIST_NULLABLE_SORT_FIELDS:
            # 空值统一垫后，避免不同数据库里空值排序行为不一致。
            return [sort_field.is_null(), ordered_field, tie_breaker]
        return [ordered_field, tie_breaker]

    @staticmethod
    def _current_time() -> datetime:
        return utc_now_for_db()

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

    @classmethod
    def _virtual_playlist_count(cls, kind: str) -> int:
        """虚拟列表的影片数实时统计，与影片列表 special_tag 过滤口径一致。"""
        # 延迟导入避免与 movie_service 顶层 import PlaylistService 形成循环依赖。
        from src.service.catalog.movie_service import MovieService

        special_tag = _VIRTUAL_KIND_TO_SPECIAL_TAG[kind]
        return MovieService._filtered_movies(special_tag=special_tag).count()

    @classmethod
    def _list_virtual_playlist_movies(
        cls,
        playlist: Playlist,
        page: int,
        page_size: int,
        *,
        sort: str | None = None,
        resolution: str | None = None,
    ) -> PageResponse[PlaylistMovieListItemResource]:
        """虚拟系统列表(VR/4K)按特殊标签实时派生成员，支持排序与分辨率筛选。

        默认按最近媒体入库时间倒序，与 materialized 列表共享同一套排序/筛选语义。
        """
        from src.service.catalog.movie_service import MovieService

        special_tag = _VIRTUAL_KIND_TO_SPECIAL_TAG[playlist.kind]
        start = max(page - 1, 0) * page_size
        base = MovieService._filtered_movies(special_tag=special_tag)
        total_query = base
        if resolution is not None:
            total_query = total_query.where(cls._resolution_exists_expression(resolution))
        total = total_query.count()
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        is_4k_expression = MovieService._special_tag_exists_expression("4K").alias("is_4k")
        latest_media_created_at = cls._latest_media_created_at_subquery()
        query, _thin_cover_alias = with_movie_card_relations(
            base.select(
                Movie,
                can_play_expression,
                is_4k_expression,
                latest_media_created_at.alias("playlist_item_updated_at"),
            )
        )
        if resolution is not None:
            query = query.where(cls._resolution_exists_expression(resolution))
        order_by = cls._build_playlist_sort(sort)
        if order_by is None:
            order_by = [Ordering(latest_media_created_at, "DESC"), Movie.id.desc()]
        movies = list(query.order_by(*order_by).offset(start).limit(page_size))
        items = [PlaylistMovieListItemResource.from_attributes_model(movie) for movie in movies]
        return PageResponse[PlaylistMovieListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

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
        # 仅对真实落库成员的列表统计 PlaylistMovie；虚拟列表的数量另行实时派生。
        materialized_ids = [
            playlist.id for playlist in playlists if playlist.kind not in cls.VIRTUAL_KINDS
        ]
        counts = cls._playlist_counts(materialized_ids)
        resources: list[PlaylistResource] = []
        for playlist in playlists:
            if playlist.kind in cls.VIRTUAL_KINDS:
                movie_count = cls._virtual_playlist_count(playlist.kind)
            else:
                movie_count = counts.get(playlist.id, 0)
            resources.append(PlaylistResource.from_playlist(playlist, movie_count=movie_count))
        return resources

    @classmethod
    def create_playlist(cls, payload: PlaylistCreateRequest) -> PlaylistResource:
        name = cls._normalize_name(payload.name)
        description = cls._normalize_description(payload.description)
        cls._ensure_name_not_reserved(name)
        cls._ensure_name_available(name)
        playlist = Playlist.create(
            name=name,
            description=description,
        )
        return PlaylistResource.from_playlist(playlist, movie_count=0)

    @classmethod
    def get_playlist(cls, playlist_id: int) -> PlaylistResource:
        playlist = cls._require_playlist(playlist_id)
        # 虚拟系统列表（VR/4K）成员不落库，走 special_tag 实时派生；其余按 PlaylistMovie 统计。
        if playlist.kind in cls.VIRTUAL_KINDS:
            movie_count = cls._virtual_playlist_count(playlist.kind)
        else:
            counts = cls._playlist_counts([playlist.id])
            movie_count = counts.get(playlist.id, 0)
        return PlaylistResource.from_playlist(playlist, movie_count=movie_count)

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

        playlist.updated_at = cls._current_time()
        playlist.save()
        counts = cls._playlist_counts([playlist.id])
        return PlaylistResource.from_playlist(playlist, movie_count=counts.get(playlist.id, 0))

    @classmethod
    def delete_playlist(cls, playlist_id: int) -> None:
        playlist = cls._require_custom_playlist(playlist_id)
        playlist.delete_instance(recursive=True)

    @classmethod
    def list_playlist_movies(
        cls,
        playlist_id: int,
        page: int = 1,
        page_size: int = 20,
        *,
        sort: str | None = None,
        resolution: str | None = None,
    ) -> PageResponse[PlaylistMovieListItemResource]:
        """列出列表内影片，支持排序(热度/码率/入库/发布时间)与分辨率筛选。

        不传 ``sort`` 时真实列表按最近触达时间倒序（与旧行为一致）。
        """
        playlist = cls._require_playlist(playlist_id)
        # VR/4K 等虚拟列表的成员不落库，按特殊标签实时派生。
        if playlist.kind in cls.VIRTUAL_KINDS:
            return cls._list_virtual_playlist_movies(
                playlist, page, page_size, sort=sort, resolution=resolution
            )
        # 先校验分辨率档位，避免非法值到查询层才炸出未预期错误。
        cls._resolution_interval(resolution)
        start = max(page - 1, 0) * page_size
        total_query = (
            PlaylistMovie.select()
            .join(Movie, on=(PlaylistMovie.movie == Movie.id))
            .where(PlaylistMovie.playlist == playlist)
        )
        if resolution is not None:
            total_query = total_query.where(cls._resolution_exists_expression(resolution))
        total = total_query.count()
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        query, _thin_cover_alias = with_movie_card_relations(
            PlaylistMovie.select(PlaylistMovie, Movie, can_play_expression)
            .join(Movie, on=(PlaylistMovie.movie == Movie.id))
            .switch(Movie)
        )
        query = query.switch(PlaylistMovie).where(PlaylistMovie.playlist == playlist)
        if resolution is not None:
            query = query.where(cls._resolution_exists_expression(resolution))
        order_by = cls._build_playlist_sort(sort)
        if order_by is None:
            order_by = [PlaylistMovie.updated_at.desc(), PlaylistMovie.id.desc()]
        links = list(query.order_by(*order_by).offset(start).limit(page_size))
        items: list[PlaylistMovieListItemResource] = []
        for link in links:
            # schema 读取的是 Movie 对象，所以把列表关系上的附加信息临时挂回 movie 实例。
            link.movie.playlist_item_updated_at = link.updated_at
            link.movie.can_play = getattr(link.movie, "can_play", getattr(link, "can_play", False))
            items.append(PlaylistMovieListItemResource.from_attributes_model(link.movie))
        return PageResponse[PlaylistMovieListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def _bucket_for_height(height: int | None) -> str | None:
        """按高度把影片归入最高命中档位；无法解析的取 None 不计入。"""
        if height is None:
            return None
        for label, threshold in RESOLUTION_LEVELS:
            if height >= threshold:
                return label
        return None

    @classmethod
    def list_playlist_resolutions(cls, playlist_id: int) -> list[PlaylistResolutionOption]:
        """聚合播放列表内影片覆盖的分辨率档位（去重、按档位从高到低），供前端渲染筛选项。

        每部影片按其最高分辨率媒体归入唯一档位（8K/4K 互斥），与筛选语义一致；
        真实列表按 PlaylistMovie 归属，虚拟列表按特殊标签派生。
        """
        playlist = cls._require_playlist(playlist_id)
        if playlist.kind in cls.VIRTUAL_KINDS:
            from src.service.catalog.movie_service import MovieService

            base = MovieService._filtered_movies(
                special_tag=_VIRTUAL_KIND_TO_SPECIAL_TAG[playlist.kind]
            )
        else:
            base = (
                Movie.select()
                .join(PlaylistMovie, on=(PlaylistMovie.movie == Movie.id))
                .where(PlaylistMovie.playlist == playlist)
            )
        max_height = fn.MAX(cls._resolution_height_expression())
        # SQL 层只按影片聚合最高 height，分桶落在 Python，避免聚合函数进 GROUP BY。
        query = (
            base.select(Movie.id, max_height.alias("max_height"))
            .join(Media, on=(Media.movie == Movie.movie_number))
            .where(Media.valid == True, Media.resolution.regexp(r"^\d+x\d+$"))
            .group_by(Movie.id)
        )
        counts: dict[str, int] = {}
        for row in query:
            label = cls._bucket_for_height(row.max_height)
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
        playlist = cls._require_custom_playlist(playlist_id)
        movie = cls._require_movie(movie_number)
        touched_at = cls._current_time()
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
    def remove_movie_from_playlist(cls, playlist_id: int, movie_number: str) -> None:
        playlist = cls._require_custom_playlist(playlist_id)
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
            cls._touch_playlist(playlist, cls._current_time())

    @classmethod
    def touch_recently_played(cls, movie: Movie) -> None:
        """把影片写入系统最近播放列表，并刷新排序时间。"""
        playlist = cls._get_or_create_recently_played_playlist()
        touched_at = cls._current_time()
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
