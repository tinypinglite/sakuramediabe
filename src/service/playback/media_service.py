from collections.abc import Sequence
from typing import Literal

import peewee
from loguru import logger

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    paginate,
    require_by_id,
    require_record,
    resolve_sort,
    resolve_sort_expression,
    validate_page,
    with_movie_card_relations,
)
from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    VideoItem,
)
from src.model.base import get_database
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
    ProviderUnavailableError,
)
from src.schema.catalog.actors import ImageResource
from src.schema.common.pagination import PageResponse
from src.schema.playback.media import (
    DuplicateMediaGroupResource,
    InvalidMediaResource,
    MediaListItemResource,
    MediaPointCreateRequest,
    MediaPointKind,
    MediaPointListItemResource,
    MediaPointResource,
    MediaProgressResource,
    MediaProgressUpdateRequest,
    MediaThumbnailGenerationState,
    MediaThumbnailResource,
)
from src.service.catalog.image_cleanup_service import ImageCleanupService

# 直接从子模块导入：collections/__init__ 会引入 clip_collection_service -> playback 形成循环，
# 绕开包级 __init__ 的初始化顺序依赖。
from src.service.collections.playlist_service import PlaylistService
from src.service.discovery import get_qdrant_thumbnail_store
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.playback.provider_helpers import media_handle_for


class MediaService:
    MEDIA_POINT_SORT_FIELDS = {
        "created_at:desc": [MediaPoint.created_at.desc(), MediaPoint.id.desc()],
        "created_at:asc": [MediaPoint.created_at.asc(), MediaPoint.id.asc()],
    }

    MEDIA_LIST_SORT_FIELD_MAP = {
        "file_size_bytes": Media.file_size_bytes,
        "heat": Movie.heat,
    }
    # 非 JAV 视频没有关联 Movie，heat 恒为空，需要排在末尾（不受排序方向影响）。
    MEDIA_LIST_NULLABLE_SORT_FIELDS = {"heat"}

    @staticmethod
    def _require_media(media_id: int) -> Media:
        return require_by_id(Media, media_id, "media", error_message="Media not found")

    @staticmethod
    def _require_media_point_for_media(media_id: int, point_id: int) -> MediaPoint:
        return require_record(
            MediaPoint, MediaPoint.id == point_id, MediaPoint.media == media_id,
            error_code="media_point_not_found",
            error_message="Media point not found",
            error_details={"media_id": media_id, "point_id": point_id},
        )

    @staticmethod
    def _to_media_point_resource(point: MediaPoint) -> MediaPointResource:
        return MediaPointResource(
            point_id=point.id,
            media_id=point.media_id,
            thumbnail_id=point.thumbnail_id,
            offset_seconds=point.offset_seconds,
            image=ImageResource.from_attributes_model(point.thumbnail.image),
            created_at=point.created_at,
        )

    @staticmethod
    def _point_query_with_thumbnail():
        return (
            MediaPoint.select(MediaPoint, MediaThumbnail, Image)
            .join(MediaThumbnail)
            .switch(MediaThumbnail)
            .join(Image)
        )

    @staticmethod
    def _require_thumbnail_for_media(media: Media, thumbnail_id: int) -> MediaThumbnail:
        return require_record(
            MediaThumbnail,
            MediaThumbnail.id == thumbnail_id,
            MediaThumbnail.media == media,
            error_code="media_thumbnail_not_found",
            error_message="Media thumbnail not found",
            error_details={"media_id": media.id, "thumbnail_id": thumbnail_id},
            query=MediaThumbnail.select(MediaThumbnail),
        )

    @classmethod
    def _resolve_media_point_sort(cls, value: str | None) -> Sequence:
        return resolve_sort(
            value, cls.MEDIA_POINT_SORT_FIELDS,
            default_key="created_at:desc", error_code="invalid_media_point_filter",
        )

    @staticmethod
    def _validate_media_point_page(page: int, page_size: int) -> None:
        validate_page(page, page_size, error_code="invalid_media_point_filter")

    @staticmethod
    def _media_point_kind_filter(kind: MediaPointKind):
        # 按归属过滤：JAV 取有番号媒体，VIDEO 取非 JAV 视频媒体，ALL 不限制。
        if kind == MediaPointKind.JAV:
            return Media.movie.is_null(False)
        if kind == MediaPointKind.VIDEO:
            return Media.video_item.is_null(False)
        return None

    @classmethod
    def _build_media_list_sort(cls, sort: str | None) -> Sequence:
        """解析 ``field:direction`` 排序表达式，默认按入库时间倒序。"""
        return resolve_sort_expression(
            sort,
            cls.MEDIA_LIST_SORT_FIELD_MAP,
            error_code="invalid_media_filter",
            nullable_fields=cls.MEDIA_LIST_NULLABLE_SORT_FIELDS,
            tie_breaker=Media.id,
            default=[Media.created_at.desc(), Media.id.desc()],
        )

    @staticmethod
    def _to_media_list_item_resource(
        media: Media,
    ) -> MediaListItemResource:
        # 按归属拆分：JAV 媒体展示番号、影片封面与热度，非 JAV 媒体回退到 VideoItem 标题。
        if media.movie_number:
            movie = media.movie
            return MediaListItemResource(
                id=media.id,
                kind="jav",
                movie_number=movie.movie_number,
                title=movie.title,
                cover_image=ImageResource.from_attributes_model(movie.cover_image)
                if movie.cover_image_id is not None
                else None,
                thin_cover_image=ImageResource.from_attributes_model(movie.thin_cover_image)
                if movie.thin_cover_image_id is not None
                else None,
                library_id=media.library_id,
                library_name=media.library.name if media.library_id is not None else None,
                file_name=media.file_name,
                file_size_bytes=media.file_size_bytes,
                duration_seconds=media.duration_seconds,
                resolution=media.resolution,
                valid=media.valid,
                thumbnail_generation_state=media.thumbnail_generation_state,
                thumbnail_last_error_code=media.thumbnail_last_error_code,
                heat=movie.heat,
                created_at=media.created_at,
                updated_at=media.updated_at,
            )
        video_item = media.video_item if media.video_item_id else None
        return MediaListItemResource(
            id=media.id,
            kind="video",
            video_item_id=media.video_item_id,
            title=video_item.title if video_item is not None else None,
            cover_image=ImageResource.from_attributes_model(video_item.cover_image)
            if video_item is not None and video_item.cover_image_id is not None
            else None,
            library_id=media.library_id,
            library_name=media.library.name if media.library_id is not None else None,
            file_name=media.file_name,
            file_size_bytes=media.file_size_bytes,
            duration_seconds=media.duration_seconds,
            resolution=media.resolution,
            valid=media.valid,
            thumbnail_generation_state=media.thumbnail_generation_state,
            thumbnail_last_error_code=media.thumbnail_last_error_code,
            heat=None,
            created_at=media.created_at,
            updated_at=media.updated_at,
        )

    @staticmethod
    def _media_list_query():
        # 非 JAV 媒体没有 movie，Movie 改为 LEFT OUTER，并补 VideoItem 兜底标题/封面。
        base_query = Media.select(Media, Movie, VideoItem).join(
            Movie,
            peewee.JOIN.LEFT_OUTER,
            on=(Media.movie == Movie.movie_number),
        )
        base_query, _thin_cover_alias = with_movie_card_relations(base_query)
        video_cover_alias = Image.alias()
        return (
            base_query.select_extend(MediaLibrary, video_cover_alias)
            .switch(Media)
            .join(VideoItem, peewee.JOIN.LEFT_OUTER)
            .join(
                video_cover_alias,
                peewee.JOIN.LEFT_OUTER,
                on=(VideoItem.cover_image == video_cover_alias.id),
                attr="cover_image",
            )
            .switch(Media)
            .join(MediaLibrary, peewee.JOIN.LEFT_OUTER)
        )

    @classmethod
    def list_media(
        cls,
        *,
        kind: MediaPointKind = MediaPointKind.ALL,
        library_id: int | None = None,
        actor_ids: list[int] | None = None,
        thumbnail_generation_state: MediaThumbnailGenerationState | None = None,
        sort: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MediaListItemResource]:
        """跨 JAV 与 videos 域分页列出全部媒体，支持归属/库/订阅女优筛选与排序。"""
        validate_page(page, page_size, error_code="invalid_media_filter")

        base_query = cls._media_list_query()

        kind_filter = cls._media_point_kind_filter(kind)
        if kind_filter is not None:
            base_query = base_query.where(kind_filter)
        if library_id is not None:
            base_query = base_query.where(Media.library == library_id)
        if actor_ids is not None:
            # MovieActor.movie 指向 Movie.id，而 Media.movie 指向 Movie.movie_number，
            # 两者不是同一标识符空间，须先转换成 movie_number 再筛 Media；
            # 用 IN 子查询而非 JOIN，避免多女优命中同一影片时主查询出现重复行，
            # 非 JAV 视频因 Media.movie 恒为 NULL 天然被排除，无需额外联动 kind。
            actor_movie_ids = MovieActor.select(MovieActor.movie).where(
                MovieActor.actor.in_(actor_ids)
            )
            movie_numbers = Movie.select(Movie.movie_number).where(
                Movie.id.in_(actor_movie_ids)
            )
            base_query = base_query.where(Media.movie.in_(movie_numbers))
        if thumbnail_generation_state is not None:
            base_query = base_query.where(
                Media.thumbnail_generation_state == thumbnail_generation_state.value
            )

        total = base_query.count()
        order_by = cls._build_media_list_sort(sort)
        rows = list(
            base_query.order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = [cls._to_media_list_item_resource(media) for media in rows]
        return PageResponse[MediaListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def list_duplicate_media_groups(
        cls,
        *,
        kind: Literal["jav", "video"],
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[DuplicateMediaGroupResource]:
        """跨媒体库按文件指纹列出同一类型的重复媒体。"""
        validate_page(page, page_size, error_code="invalid_media_filter")
        kind_filter = (
            Media.movie.is_null(False)
            if kind == "jav"
            else Media.video_item.is_null(False)
        )
        duplicate_hashes_query = (
            Media.select(Media.file_hash)
            .where(
                kind_filter,
                Media.file_hash.is_null(False),
                Media.file_hash != "",
            )
            .group_by(Media.file_hash)
            .having(peewee.fn.COUNT(Media.id) > 1)
        )
        total = duplicate_hashes_query.count()
        page_hashes = [
            file_hash
            for (file_hash,) in (
                duplicate_hashes_query.order_by(
                    peewee.fn.MAX(Media.updated_at).desc(),
                    Media.file_hash.asc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
                .tuples()
            )
        ]
        if not page_hashes:
            return PageResponse[DuplicateMediaGroupResource](
                items=[], page=page, page_size=page_size, total=total
            )

        media_by_hash: dict[str, list[MediaListItemResource]] = {
            file_hash: [] for file_hash in page_hashes
        }
        rows = (
            cls._media_list_query()
            .where(kind_filter, Media.file_hash.in_(page_hashes))
            .order_by(Media.file_hash.asc(), Media.created_at.asc(), Media.id.asc())
        )
        for media in rows:
            media_by_hash[media.file_hash].append(
                cls._to_media_list_item_resource(media)
            )

        return PageResponse[DuplicateMediaGroupResource](
            items=[
                DuplicateMediaGroupResource(
                    kind=kind,
                    media_count=len(media_by_hash[file_hash]),
                    media_items=media_by_hash[file_hash],
                )
                for file_hash in page_hashes
            ],
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def list_media_points(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str | None = None,
        kind: MediaPointKind = MediaPointKind.JAV,
    ) -> PageResponse[MediaPointListItemResource]:
        cls._validate_media_point_page(page, page_size)
        start = (page - 1) * page_size
        order_by = cls._resolve_media_point_sort(sort)
        kind_filter = cls._media_point_kind_filter(kind)
        # total 与分页查询套用同一 kind 过滤，需 join Media 才能按归属筛。
        total_query = MediaPoint.select().join(Media)
        if kind_filter is not None:
            total_query = total_query.where(kind_filter)
        total = total_query.count()
        points_query = (
            MediaPoint.select(MediaPoint, Media, Movie, MediaThumbnail, Image)
            .join(Media)
            .switch(Media)
            # 非 JAV 媒体没有 movie，改为 LEFT OUTER JOIN 让两类时刻都能列出。
            .join(Movie, peewee.JOIN.LEFT_OUTER, on=(Media.movie == Movie.movie_number))
            .switch(MediaPoint)
            .join(MediaThumbnail)
            .switch(MediaThumbnail)
            .join(Image)
        )
        if kind_filter is not None:
            points_query = points_query.where(kind_filter)
        points = list(
            points_query
            .order_by(*order_by)
            .offset(start)
            .limit(page_size)
        )
        items = [
            MediaPointListItemResource(
                point_id=point.id,
                media_id=point.media_id,
                movie_number=point.media.movie_number,
                video_item_id=point.media.video_item_id,
                thumbnail_id=point.thumbnail_id,
                offset_seconds=point.offset_seconds,
                image=ImageResource.from_attributes_model(point.thumbnail.image),
                created_at=point.created_at,
            )
            for point in points
        ]
        return PageResponse[MediaPointListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def list_points(cls, media_id: int) -> list[MediaPointResource]:
        cls._require_media(media_id)
        points = (
            cls._point_query_with_thumbnail()
            .where(MediaPoint.media == media_id)
            .order_by(MediaPoint.id)
        )
        return [cls._to_media_point_resource(point) for point in points]

    @classmethod
    def create_point(
        cls,
        media_id: int,
        payload: MediaPointCreateRequest,
    ) -> tuple[MediaPointResource, bool]:
        media = cls._require_media(media_id)
        thumbnail = cls._require_thumbnail_for_media(media, payload.thumbnail_id)
        with get_database().atomic():
            point = (
                MediaPoint.select()
                .where(
                    MediaPoint.media == media,
                    MediaPoint.thumbnail == thumbnail,
                )
                .order_by(MediaPoint.id)
                .first()
            )
            if point is not None:
                point = cls._point_query_with_thumbnail().where(MediaPoint.id == point.id).get()
                return cls._to_media_point_resource(point), False

            point = MediaPoint.create(
                media=media,
                thumbnail=thumbnail,
                offset_seconds=thumbnail.offset,
            )
            point = cls._point_query_with_thumbnail().where(MediaPoint.id == point.id).get()
        return cls._to_media_point_resource(point), True

    @classmethod
    def delete_point(cls, media_id: int, point_id: int) -> None:
        cls._require_media(media_id)
        point = cls._require_media_point_for_media(media_id, point_id)
        point.delete_instance()

    @classmethod
    def update_progress(
        cls,
        media_id: int,
        payload: MediaProgressUpdateRequest,
    ) -> MediaProgressResource:
        media = cls._require_media(media_id)
        watched_at = utc_now_for_db()
        progress = MediaProgress.get_or_none(MediaProgress.media == media)
        if progress is None:
            progress = MediaProgress.create(
                media=media,
                position_seconds=payload.position_seconds,
                last_watched_at=watched_at,
                created_at=watched_at,
                updated_at=watched_at,
            )
        else:
            progress.position_seconds = payload.position_seconds
            progress.last_watched_at = watched_at
            progress.updated_at = watched_at
            progress.save()

        # 最近播放列表是 JAV 影片维度的能力，非 JAV 媒体跳过维护。
        if media.movie_number:
            PlaylistService.touch_recently_played(media.movie)
        return MediaProgressResource(
            media_id=media.id,
            last_position_seconds=progress.position_seconds,
            last_watched_at=progress.last_watched_at,
        )

    @classmethod
    def delete_media(cls, media_id: int) -> None:
        media = cls._require_media(media_id)
        media_handle = media_handle_for(media)
        try:
            storage = MEDIA_PROVIDER_REGISTRY.storage_for(media_handle.library)
            storage.delete_media(media=media_handle)
        except ProviderUnavailableError as exc:
            raise ApiError(
                503,
                "provider_not_installed",
                "媒体提供方未安装",
            ) from exc
        except ProviderOperationError as exc:
            if exc.code == "source_not_found":
                # Provider 已确认来源不存在，宿主记录已无对应远端对象，继续清理本地元数据。
                pass
            else:
                status_code = {
                    "authentication_failed": 401,
                    "unavailable": 503,
                    "invalid_config": 422,
                    "unsupported": 422,
                }[exc.code]
                raise ApiError(
                    status_code,
                    f"provider_{exc.code}",
                    exc.safe_message,
                ) from exc
        thumbnails = list(
            MediaThumbnail.select(MediaThumbnail, Image)
            .join(Image)
            .where(MediaThumbnail.media == media)
        )
        thumbnail_image_ids = [thumbnail.image_id for thumbnail in thumbnails]

        with get_database().atomic():
            # 依赖 DB 外键 CASCADE 自动清 MediaProgress / MediaPoint / MediaThumbnail。
            Media.delete().where(Media.id == media.id).execute()

            obsolete_image_paths: set[str] = set()
            for image_id in thumbnail_image_ids:
                image = Image.get_or_none(Image.id == image_id)
                obsolete_image_paths |= ImageCleanupService.delete_image_record_if_unused(image)

        ImageCleanupService.delete_obsolete_image_files(obsolete_image_paths)

        # 仅 JAV 媒体缩略图会进向量库；非 JAV 缩略图落 SKIPPED 从不入库，跳过空删省一次远端往返。
        if media.movie_number:
            try:
                get_qdrant_thumbnail_store().delete_by_media_id(media.id)
            except Exception as exc:
                logger.warning("Delete media vectors failed media_id={} detail={}", media.id, exc)

    @classmethod
    def list_thumbnails(cls, media_id: int) -> list[MediaThumbnailResource]:
        cls._require_media(media_id)
        return MediaThumbnailService.list_media_thumbnails(media_id)

    @staticmethod
    def _to_invalid_media_resource(media: Media) -> InvalidMediaResource:
        # 按归属拆分：JAV 媒体展示番号与影片封面，非 JAV 媒体回退到 VideoItem 标题。
        if media.movie_number:
            movie = media.movie
            return InvalidMediaResource(
                id=media.id,
                movie_number=movie.movie_number,
                movie_title=movie.title,
                cover_image=ImageResource.from_attributes_model(movie.cover_image)
                if movie.cover_image_id is not None
                else None,
                thin_cover_image=ImageResource.from_attributes_model(movie.thin_cover_image)
                if movie.thin_cover_image_id is not None
                else None,
                file_name=media.file_name,
                library_id=media.library_id,
                library_name=media.library.name if media.library_id is not None else None,
                file_size_bytes=media.file_size_bytes,
                updated_at=media.updated_at,
            )
        video_item = media.video_item if media.video_item_id else None
        return InvalidMediaResource(
            id=media.id,
            video_item_id=media.video_item_id,
            movie_title=video_item.title if video_item is not None else None,
            cover_image=ImageResource.from_attributes_model(video_item.cover_image)
            if video_item is not None and video_item.cover_image_id is not None
            else None,
            file_name=media.file_name,
            library_id=media.library_id,
            library_name=media.library.name if media.library_id is not None else None,
            file_size_bytes=media.file_size_bytes,
            updated_at=media.updated_at,
        )

    @classmethod
    def list_invalid_media(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
    ) -> PageResponse[InvalidMediaResource]:
        validate_page(page, page_size, error_code="invalid_media_filter")
        # 非 JAV 媒体没有 movie，Movie 改为 LEFT OUTER，并补 VideoItem 兜底标题。
        base_query = Media.select(Media, Movie, VideoItem).join(
            Movie,
            peewee.JOIN.LEFT_OUTER,
            on=(Media.movie == Movie.movie_number),
        )
        # 失效媒体卡片需要展示影片横版与竖版封面，沿用影片卡片的关联加载逻辑。
        base_query, _thin_cover_alias = with_movie_card_relations(base_query)
        # 非 JAV 失效媒体封面来自 VideoItem.cover_image，用别名再 LEFT JOIN 一张 Image 预加载，
        # 避免 _to_invalid_media_resource 逐行懒加载 video_item.cover_image（N+1）。
        video_cover_alias = Image.alias()
        base_query = (
            base_query.select_extend(MediaLibrary, video_cover_alias)
            .switch(Media)
            .join(VideoItem, peewee.JOIN.LEFT_OUTER)
            .join(
                video_cover_alias,
                peewee.JOIN.LEFT_OUTER,
                on=(VideoItem.cover_image == video_cover_alias.id),
                attr="cover_image",
            )
            .switch(Media)
            .join(MediaLibrary, peewee.JOIN.LEFT_OUTER)
            .where(Media.valid == False)
        )
        normalized = (search or "").strip()
        if normalized:
            base_query = base_query.where(
                (Movie.movie_number.contains(normalized))
                | (Movie.title.contains(normalized))
                | (VideoItem.title.contains(normalized))
                | (Media.file_name.contains(normalized))
            )
        return paginate(
            base_query.order_by(Media.updated_at.desc(), Media.id.desc()),
            page,
            page_size,
            error_code="invalid_media_filter",
            item_mapper=cls._to_invalid_media_resource,
            response_model=PageResponse[InvalidMediaResource],
        )
