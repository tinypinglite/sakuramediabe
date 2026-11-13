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
    RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    RECENTLY_PLAYED_PLAYLIST_NAME,
    SYSTEM_PLAYLIST_KINDS,
    Media,
    Movie,
    Playlist,
    PlaylistMovie,
    get_database,
)
from src.schema.collections.playlists import (
    PlaylistCreateRequest,
    PlaylistMovieBatchAddResponse,
    PlaylistMovieBatchAddSkippedItem,
    PlaylistMovieListItemResource,
    PlaylistResolutionOption,
    PlaylistResource,
    PlaylistUpdateRequest,
)
from src.schema.common.pagination import PageResponse
from src.schema.common.playlists import PlaylistSummaryResource
from src.service.catalog.movie_list_media_service import attach_movie_list_media
from src.service.catalog.movie_resolution_service import (
    RESOLUTION_LEVELS,
    resolution_exists_expression,
    resolution_interval,
    resolution_level_expression,
)

# 系统列表内部展示次序：收藏在前，最近播放在后，自定义列表最后。
_SYSTEM_KIND_ORDER = (
    (PLAYLIST_KIND_FAVORITES, 0),
    (PLAYLIST_KIND_RECENTLY_PLAYED, 1),
)

# 允许空值、排序时统一垫后的字段（added_at/bitrate 走子查询无媒体场景由 COALESCE 兜底，不参与垫后）。
PLAYLIST_NULLABLE_SORT_FIELDS = {"release_date"}


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

    @classmethod
    def _build_playlist_sort(cls, sort: str | None):
        """解析 ``field:direction`` 播放列表排序，并补上稳定的次级排序；无效值抛 422。

        bitrate / added_at 用相关子查询（影片粒度），heat / release_date 取 Movie 列。
        """
        def _subquery_order(field_name: str, direction: str) -> list:
            # added_at / bitrate 均以影片粒度相关子查询为排序列。
            sort_field = (
                cls._latest_media_created_at_subquery()
                if field_name == "added_at"
                else cls._max_bitrate_subquery()
            )
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
            },
            error_code="invalid_playlist_filter",
            nullable_fields=PLAYLIST_NULLABLE_SORT_FIELDS,
            tie_breaker=Movie.id,
            default=None,
            extra_sort_builders={"added_at": _subquery_order, "bitrate": _subquery_order},
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
        """允许自定义列表和收藏增删影片；最近播放仍由系统维护。"""
        playlist = cls._require_playlist(playlist_id)
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
            resources.append(
                PlaylistResource.from_playlist(playlist, movie_count=counts.get(playlist.id, 0))
            )
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
        counts = cls._playlist_counts([playlist.id])
        return PlaylistResource.from_playlist(playlist, movie_count=counts.get(playlist.id, 0))

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

        playlist.updated_at = utc_now_for_db()
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
        # 先校验分辨率档位，避免非法值到查询层才炸出未预期错误。
        resolution_interval(resolution)
        start = max(page - 1, 0) * page_size
        total_query = (
            PlaylistMovie.select()
            .join(Movie, on=(PlaylistMovie.movie == Movie.id))
            .where(PlaylistMovie.playlist == playlist)
        )
        if resolution is not None:
            total_query = total_query.where(resolution_exists_expression(resolution))
        total = total_query.count()
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        query, _thin_cover_alias = with_movie_card_relations(
            PlaylistMovie.select(PlaylistMovie, Movie, can_play_expression)
            .join(Movie, on=(PlaylistMovie.movie == Movie.id))
            .switch(Movie)
        )
        query = query.switch(PlaylistMovie).where(PlaylistMovie.playlist == playlist)
        if resolution is not None:
            query = query.where(resolution_exists_expression(resolution))
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
