"""插件合集的归属校验与批量成员替换。"""

from collections.abc import Collection

from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import find_movie_by_number
from src.model import (
    PLAYLIST_KIND_CUSTOM,
    ClipCollection,
    MomentCollection,
    Playlist,
    PlaylistMovie,
)
from src.model.base import get_database
from src.service.collections.clip_collection_service import (
    ClipCollectionService,
)
from src.service.collections.moment_collection_service import (
    MomentCollectionService,
)


class PluginCollectionService:
    """只给插件 facade 提供按 ``plugin_id + key`` 定位的合集操作。"""

    @staticmethod
    def _validate_key(plugin_key: str) -> str:
        key = (plugin_key or "").strip()
        if not key:
            raise ValueError("plugin_key 不能为空")
        if len(key) > 128:
            raise ValueError("plugin_key 不能超过 128 个字符")
        return key

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = (name or "").strip()
        if not normalized:
            raise ValueError("合集名称不能为空")
        if len(normalized) > 255:
            raise ValueError("合集名称不能超过 255 个字符")
        return normalized

    @staticmethod
    def _normalize_description(description: str | None) -> str:
        return (description or "").strip()

    @classmethod
    def _ensure_collection(
        cls,
        model,
        *,
        plugin_id: str,
        plugin_key: str,
        name: str,
        description: str | None,
        **defaults,
    ):
        plugin_key = cls._validate_key(plugin_key)
        name = cls._normalize_name(name)
        description = cls._normalize_description(description)
        collection = model.get_or_none(
            (model.owner_plugin_id == plugin_id) & (model.plugin_key == plugin_key)
        )
        if collection is not None:
            changed = collection.name != name or collection.description != description
            if changed:
                collection.name = name
                collection.description = description
                try:
                    collection.save(only=[model.name, model.description])
                except IntegrityError as exc:
                    raise ApiError(
                        409,
                        "plugin_collection_conflict",
                        "插件合集名称已存在",
                        {"plugin_key": plugin_key, "name": name},
                    ) from exc
            return collection

        try:
            return model.create(
                **defaults,
                name=name,
                description=description,
                owner_plugin_id=plugin_id,
                plugin_key=plugin_key,
            )
        except IntegrityError as exc:
            # 名称唯一约束或并发创建冲突都转成插件可理解的稳定错误。
            raise ApiError(
                409,
                "plugin_collection_conflict",
                "插件合集名称或 key 已存在",
                {"plugin_key": plugin_key, "name": name},
            ) from exc

    @staticmethod
    def _require_owned(model, *, plugin_id: str, plugin_key: str):
        plugin_key = PluginCollectionService._validate_key(plugin_key)
        collection = model.get_or_none(
            (model.owner_plugin_id == plugin_id) & (model.plugin_key == plugin_key)
        )
        if collection is None:
            raise ApiError(
                404,
                "plugin_collection_not_found",
                "插件合集不存在",
                {"plugin_key": plugin_key},
            )
        return collection

    @classmethod
    def ensure_playlist(
        cls, plugin_id: str, plugin_key: str, name: str, description: str | None = None
    ) -> Playlist:
        return cls._ensure_collection(
            Playlist,
            plugin_id=plugin_id,
            plugin_key=plugin_key,
            name=name,
            description=description,
            kind=PLAYLIST_KIND_CUSTOM,
        )

    @classmethod
    def set_playlist_movies(
        cls,
        plugin_id: str,
        plugin_key: str,
        movie_numbers: Collection[str],
    ) -> Playlist:
        playlist = cls._require_owned(Playlist, plugin_id=plugin_id, plugin_key=plugin_key)
        movies = []
        seen_ids: set[int] = set()
        for raw_number in movie_numbers:
            number = (raw_number or "").strip()
            if not number:
                raise ValueError("movie_number 不能为空")
            movie = find_movie_by_number(number)
            if movie is None:
                raise ApiError(
                    404,
                    "movie_not_found",
                    "影片不存在",
                    {"movie_number": number},
                )
            if movie.id not in seen_ids:
                seen_ids.add(movie.id)
                movies.append(movie)

        touched_at = utc_now_for_db()
        with get_database().atomic():
            PlaylistMovie.delete().where(PlaylistMovie.playlist == playlist).execute()
            for movie in movies:
                PlaylistMovie.create(
                    playlist=playlist,
                    movie=movie,
                    created_at=touched_at,
                    updated_at=touched_at,
                )
            playlist.updated_at = touched_at
            playlist.save(only=[Playlist.updated_at])
        return playlist

    @classmethod
    def ensure_moment(
        cls, plugin_id: str, plugin_key: str, name: str, description: str | None = None
    ) -> MomentCollection:
        return cls._ensure_collection(
            MomentCollection,
            plugin_id=plugin_id,
            plugin_key=plugin_key,
            name=name,
            description=description,
        )

    @classmethod
    def set_moment_points(
        cls,
        plugin_id: str,
        plugin_key: str,
        point_ids: Collection[int],
    ) -> MomentCollection:
        collection = cls._require_owned(
            MomentCollection, plugin_id=plugin_id, plugin_key=plugin_key
        )
        MomentCollectionService.set_points(collection.id, list(point_ids))
        return collection

    @classmethod
    def ensure_clip(
        cls, plugin_id: str, plugin_key: str, name: str, description: str | None = None
    ) -> ClipCollection:
        return cls._ensure_collection(
            ClipCollection,
            plugin_id=plugin_id,
            plugin_key=plugin_key,
            name=name,
            description=description,
        )

    @classmethod
    def set_clip_clips(
        cls,
        plugin_id: str,
        plugin_key: str,
        clip_ids: Collection[int],
    ) -> ClipCollection:
        collection = cls._require_owned(
            ClipCollection, plugin_id=plugin_id, plugin_key=plugin_key
        )
        ClipCollectionService.set_clips(collection.id, list(clip_ids))
        return collection
