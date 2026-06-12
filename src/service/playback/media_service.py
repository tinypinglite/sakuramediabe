from datetime import datetime
from pathlib import Path
from typing import Sequence

import peewee
from loguru import logger
from src.api.exception.errors import ApiError
from src.common.service_helpers import (
    require_record,
    resolve_sort,
    validate_page,
    with_movie_card_relations,
)
from src.common.runtime_time import utc_now_for_db
from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    ResourceTaskState,
    VideoItem,
)
from src.model.base import get_database
from src.schema.catalog.actors import ImageResource
from src.schema.common.pagination import PageResponse
from src.schema.playback.media import (
    InvalidMediaResource,
    MediaValidityCheckResponse,
    MediaPointCreateRequest,
    MediaPointListItemResource,
    MediaPointResource,
    MediaProgressResource,
    MediaProgressUpdateRequest,
    MediaThumbnailResource,
)
from src.service.catalog.image_cleanup_service import ImageCleanupService
from src.service.collections import PlaylistService
from src.service.discovery import get_qdrant_thumbnail_store
from src.service.playback.media_file_scan_service import MediaFileScanService
from src.service.playback.media_thumbnail_service import MediaThumbnailService


class MediaService:
    MEDIA_POINT_SORT_FIELDS = {
        "created_at:desc": [MediaPoint.created_at.desc(), MediaPoint.id.desc()],
        "created_at:asc": [MediaPoint.created_at.asc(), MediaPoint.id.asc()],
    }

    @staticmethod
    def _current_time() -> datetime:
        return utc_now_for_db()

    @staticmethod
    def _require_media(media_id: int) -> Media:
        return require_record(
            Media, Media.id == media_id,
            error_code="media_not_found",
            error_message="Media not found",
            error_details={"media_id": media_id},
        )

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
    def _delete_local_media_file(media: Media) -> None:
        try:
            Path(media.path).unlink()
        except FileNotFoundError:
            return

    @classmethod
    def list_media_points(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str | None = None,
    ) -> PageResponse[MediaPointListItemResource]:
        cls._validate_media_point_page(page, page_size)
        start = (page - 1) * page_size
        order_by = cls._resolve_media_point_sort(sort)
        total = MediaPoint.select().count()
        points = list(
            MediaPoint.select(MediaPoint, Media, Movie, MediaThumbnail, Image)
            .join(Media)
            .switch(Media)
            # 非 JAV 媒体没有 movie，改为 LEFT OUTER JOIN 让两类时刻都能列出。
            .join(Movie, peewee.JOIN.LEFT_OUTER, on=(Media.movie == Movie.movie_number))
            .switch(MediaPoint)
            .join(MediaThumbnail)
            .switch(MediaThumbnail)
            .join(Image)
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
        watched_at = cls._current_time()
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

        thumbnails = list(
            MediaThumbnail.select(MediaThumbnail, Image)
            .join(Image)
            .where(MediaThumbnail.media == media)
        )
        thumbnail_image_ids = [thumbnail.image_id for thumbnail in thumbnails]

        cls._delete_local_media_file(media)

        with get_database().atomic():
            # 依赖 DB 外键 CASCADE 自动清 MediaProgress / MediaPoint / MediaThumbnail。
            Media.delete().where(Media.id == media.id).execute()

            obsolete_image_paths: set[str] = set()
            for image_id in thumbnail_image_ids:
                image = Image.get_or_none(Image.id == image_id)
                obsolete_image_paths |= ImageCleanupService.delete_image_record_if_unused(image)

            ResourceTaskState.delete().where(
                ResourceTaskState.resource_type == "media",
                ResourceTaskState.resource_id == media.id,
            ).execute()

        ImageCleanupService.delete_obsolete_image_files(obsolete_image_paths)

        try:
            get_qdrant_thumbnail_store().delete_by_media_id(media.id)
        except Exception as exc:
            logger.warning("Delete media vectors failed media_id={} detail={}", media.id, exc)

    @classmethod
    def list_thumbnails(cls, media_id: int) -> list[MediaThumbnailResource]:
        cls._require_media(media_id)
        return MediaThumbnailService.list_media_thumbnails(media_id)

    @classmethod
    def check_media_validity(cls, media_id: int) -> MediaValidityCheckResponse:
        cls._require_media(media_id)
        result = MediaFileScanService().check_media_file(media_id)
        return MediaValidityCheckResponse(
            id=result.id,
            path=result.path,
            file_exists=result.file_exists,
            valid_before=result.valid_before,
            valid_after=result.valid_after,
            updated=result.updated,
            invalidated=result.invalidated,
            revived=result.revived,
            checked_at=result.checked_at,
        )

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
                path=media.path,
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
            path=media.path,
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
        base_query = (
            base_query.select_extend(MediaLibrary)
            .switch(Media)
            .join(VideoItem, peewee.JOIN.LEFT_OUTER)
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
                | (Media.path.contains(normalized))
            )
        total = base_query.count()
        rows = list(
            base_query.order_by(Media.updated_at.desc(), Media.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = [cls._to_invalid_media_resource(media) for media in rows]
        return PageResponse[InvalidMediaResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )
