"""播放列表 service。

负责自定义播放列表和系统播放列表的增删改查，以及影片和播放列表之间的关系维护。
阅读入口建议从 ``list_playlists``、``list_playlist_movies``、``touch_recently_played`` 开始。
"""

from datetime import datetime
from typing import Dict, List

from peewee import JOIN, Case, fn

from src.api.exception.errors import ApiError
from src.model import (
    Image,
    Media,
    Movie,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    PlaylistMovie,
    RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    RECENTLY_PLAYED_PLAYLIST_NAME,
)
from src.schema.collections.playlists import (
    PlaylistCreateRequest,
    PlaylistMovieListItemResource,
    PlaylistResource,
    PlaylistUpdateRequest,
)
from src.schema.common.pagination import PageResponse
from src.schema.common.playlists import PlaylistSummaryResource


class PlaylistService:
    """聚合播放列表查询、名称校验和最近播放维护逻辑。"""

    SYSTEM_KINDS = {PLAYLIST_KIND_RECENTLY_PLAYED}
    RESERVED_NAMES = {RECENTLY_PLAYED_PLAYLIST_NAME}

    @staticmethod
    def _playlist_system_order():
        """让系统播放列表固定排在普通列表之前。"""
        return Case(
            None,
            ((Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED, 0),),
            1,
        )

    @staticmethod
    def _movie_playlist_system_order():
        """列出影片所属播放列表时同样优先展示系统列表。"""
        return Case(
            None,
            ((Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED, 0),),
            1,
        )

    @staticmethod
    def _playable_exists_expression():
        """返回“影片是否存在可播放媒体”的子查询表达式。"""
        playable_media = Media.select(Media.id).where(
            Media.valid == True,
            Media.movie == Movie.movie_number,
        )
        return fn.EXISTS(playable_media)

    @staticmethod
    def _current_time() -> datetime:
        return datetime.utcnow()

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
        playlist = Playlist.get_or_none(Playlist.id == playlist_id)
        if playlist is None:
            raise ApiError(
                404,
                "playlist_not_found",
                "Playlist not found",
                {"playlist_id": playlist_id},
            )
        return playlist

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
        movie = Movie.get_or_none(Movie.movie_number == movie_number)
        if movie is None:
            raise ApiError(
                404,
                "movie_not_found",
                "Movie not found",
                {"movie_number": movie_number},
            )
        return movie

    @staticmethod
    def _touch_playlist(playlist: Playlist, touched_at: datetime) -> None:
        playlist.updated_at = touched_at
        playlist.save(only=[Playlist.updated_at])

    @classmethod
    def _playlist_counts(cls, playlist_ids: List[int]) -> Dict[int, int]:
        if not playlist_ids:
            return {}
        query = (
            PlaylistMovie.select(PlaylistMovie.playlist, fn.COUNT(PlaylistMovie.id).alias("movie_count"))
            .where(PlaylistMovie.playlist.in_(playlist_ids))
            .group_by(PlaylistMovie.playlist)
        )
        return {item.playlist_id: item.movie_count for item in query}

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
    def list_playlists(cls, include_system: bool = True) -> List[PlaylistResource]:
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
        return [
            PlaylistResource.from_playlist(playlist, movie_count=counts.get(playlist.id, 0))
            for playlist in playlists
        ]

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
    ) -> PageResponse[PlaylistMovieListItemResource]:
        """按最近触达时间列出列表内影片，并补上可播放状态。"""
        playlist = cls._require_playlist(playlist_id)
        start = max(page - 1, 0) * page_size
        total = PlaylistMovie.select().where(PlaylistMovie.playlist == playlist).count()
        can_play_expression = cls._playable_exists_expression().alias("can_play")
        links = list(
            PlaylistMovie.select(PlaylistMovie, Movie, Image, can_play_expression)
            .join(Movie, on=(PlaylistMovie.movie == Movie.id))
            .switch(Movie)
            .join(Image, JOIN.LEFT_OUTER, on=(Movie.cover_image == Image.id))
            .switch(PlaylistMovie)
            .where(PlaylistMovie.playlist == playlist)
            .order_by(PlaylistMovie.updated_at.desc(), PlaylistMovie.id.desc())
            .offset(start)
            .limit(page_size)
        )
        items: List[PlaylistMovieListItemResource] = []
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
    def list_movie_playlists(cls, movie: Movie) -> List[PlaylistSummaryResource]:
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
