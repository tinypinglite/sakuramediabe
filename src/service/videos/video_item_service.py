"""视频条目（VideoItem）service：非 JAV 视频的条目增删改查与详情组装。"""

from datetime import datetime
from typing import Dict, List

from peewee import JOIN, Case, fn

from src.api.exception.errors import ApiError
from src.common import build_signed_media_url
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import require_record, validate_page
from src.model import (
    Image,
    Media,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Person,
    Tag,
    VideoItem,
    VideoItemPerson,
    VideoItemTag,
)
from src.model.base import get_database
from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import (
    MovieMediaPointResource,
    MovieMediaProgressResource,
    MovieMediaResource,
    TagResource,
)
from src.schema.common.pagination import PageResponse
from src.schema.videos.items import (
    VideoItemCreateRequest,
    VideoItemDetailResource,
    VideoItemListItemResource,
    VideoItemUpdateRequest,
    VideoPersonResource,
)

# 视频条目列表允许的排序字段。
_VIDEO_SORT_FIELDS = {
    "created_at": VideoItem.created_at,
    "release_date": VideoItem.release_date,
    "title": VideoItem.title,
}


class VideoItemService:
    @staticmethod
    def _current_time() -> datetime:
        return utc_now_for_db()

    @staticmethod
    def _require_video(video_id: int) -> VideoItem:
        return require_record(
            VideoItem,
            VideoItem.id == video_id,
            error_code="video_item_not_found",
            error_message="Video item not found",
            error_details={"video_item_id": video_id},
        )

    @classmethod
    def _build_sort(cls, sort: str | None):
        normalized = (sort or "created_at:desc").strip().lower()
        try:
            field_name, direction = normalized.split(":", 1)
        except ValueError as exc:
            raise ApiError(422, "invalid_video_filter", "Invalid video sort", {"sort": sort}) from exc
        if field_name not in _VIDEO_SORT_FIELDS or direction not in ("asc", "desc"):
            raise ApiError(422, "invalid_video_filter", "Invalid video sort", {"sort": sort})
        sort_field = _VIDEO_SORT_FIELDS[field_name]
        ordered = sort_field.asc() if direction == "asc" else sort_field.desc()
        tie_breaker = VideoItem.id.asc() if direction == "asc" else VideoItem.id.desc()
        return [ordered, tie_breaker]

    @classmethod
    def _filtered_query(
        cls,
        *,
        query: str | None = None,
        tag_ids: List[int] | None = None,
        person_ids: List[int] | None = None,
    ):
        video_query = VideoItem.select(VideoItem)
        if tag_ids:
            video_query = (
                video_query.join(VideoItemTag, on=(VideoItemTag.video_item == VideoItem.id))
                .where(VideoItemTag.tag.in_(tag_ids))
                .switch(VideoItem)
            )
        if person_ids:
            video_query = (
                video_query.join(VideoItemPerson, on=(VideoItemPerson.video_item == VideoItem.id))
                .where(VideoItemPerson.person.in_(person_ids))
                .switch(VideoItem)
            )
        if query is not None:
            normalized = query.strip()
            if not normalized:
                raise ApiError(422, "invalid_video_filter", "Invalid video filter", {"query": query})
            video_query = video_query.where(VideoItem.title.contains(normalized))
        # tag/person 多对多 join 可能放大行，统一去重。
        return video_query.distinct()

    @staticmethod
    def _media_stats(video_ids: List[int]) -> Dict[int, tuple[int, bool]]:
        """批量统计每个视频的媒体数量与是否存在可播放媒体。"""
        if not video_ids:
            return {}
        # 用 CASE 折算 valid 计数，避免 PostgreSQL 不支持 SUM(boolean)。
        valid_count = fn.SUM(Case(None, [(Media.valid == True, 1)], 0))
        rows = (
            Media.select(
                Media.video_item,
                fn.COUNT(Media.id).alias("media_count"),
                valid_count.alias("valid_count"),
            )
            .where(Media.video_item.in_(video_ids))
            .group_by(Media.video_item)
        )
        stats: Dict[int, tuple[int, bool]] = {}
        for row in rows:
            stats[row.video_item_id] = (row.media_count, bool(row.valid_count))
        return stats

    @classmethod
    def _to_list_item(cls, video: VideoItem, media_count: int, can_play: bool) -> VideoItemListItemResource:
        return VideoItemListItemResource(
            id=video.id,
            title=video.title,
            summary=video.summary,
            cover_image=ImageResource.from_attributes_model(video.cover_image)
            if video.cover_image_id is not None
            else None,
            release_date=video.release_date,
            media_count=media_count,
            can_play=can_play,
            created_at=video.created_at,
            updated_at=video.updated_at,
        )

    @classmethod
    def list_videos(
        cls,
        *,
        query: str | None = None,
        tag_ids: List[int] | None = None,
        person_ids: List[int] | None = None,
        sort: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[VideoItemListItemResource]:
        validate_page(page, page_size, error_code="invalid_video_filter")
        order_by = cls._build_sort(sort)
        base_query = cls._filtered_query(query=query, tag_ids=tag_ids, person_ids=person_ids)
        total = base_query.count()
        videos = list(
            base_query.order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        stats = cls._media_stats([video.id for video in videos])
        items = [
            cls._to_list_item(video, *stats.get(video.id, (0, False)))
            for video in videos
        ]
        return PageResponse[VideoItemListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def _video_tags(video: VideoItem) -> List[TagResource]:
        tags = (
            Tag.select(Tag)
            .join(VideoItemTag)
            .where(VideoItemTag.video_item == video)
            .order_by(Tag.id)
        )
        return [TagResource(tag_id=tag.id, name=tag.name) for tag in tags]

    @staticmethod
    def _video_persons(video: VideoItem) -> List[VideoPersonResource]:
        persons = (
            Person.select(Person)
            .join(VideoItemPerson)
            .where(VideoItemPerson.video_item == video)
            .order_by(Person.id)
        )
        return [
            VideoPersonResource(
                id=person.id,
                name=person.name,
                avatar_image=ImageResource.from_attributes_model(person.avatar_image)
                if person.avatar_image_id is not None
                else None,
            )
            for person in persons
        ]

    @staticmethod
    def _media_items(video: VideoItem) -> List[MovieMediaResource]:
        """组装视频详情页的媒体列表，复用影片媒体资源结构（进度 + 时刻）。"""
        media_items = list(
            Media.select(Media).where(Media.video_item == video).order_by(Media.id)
        )
        if not media_items:
            return []
        media_ids = [media.id for media in media_items]
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
            points_by_media_id.setdefault(point.media_id, []).append(
                MovieMediaPointResource(
                    point_id=point.id,
                    thumbnail_id=point.thumbnail_id,
                    offset_seconds=point.offset_seconds,
                    image=ImageResource.from_attributes_model(point.thumbnail.image),
                )
            )
        resources: List[MovieMediaResource] = []
        for media in media_items:
            progress = progress_items.get(media.id)
            media.progress = (
                None
                if progress is None
                else MovieMediaProgressResource(
                    last_position_seconds=progress.position_seconds,
                    last_watched_at=progress.last_watched_at,
                )
            )
            media.points = points_by_media_id.get(media.id, [])
            media.play_url = build_signed_media_url(media.id)
            resources.append(MovieMediaResource.from_attributes_model(media))
        return resources

    @classmethod
    def get_video_detail(cls, video_id: int) -> VideoItemDetailResource:
        video = cls._require_video(video_id)
        media_items = cls._media_items(video)
        stats_media_count = len(media_items)
        can_play = any(media.valid for media in media_items)
        return VideoItemDetailResource(
            id=video.id,
            title=video.title,
            summary=video.summary,
            cover_image=ImageResource.from_attributes_model(video.cover_image)
            if video.cover_image_id is not None
            else None,
            release_date=video.release_date,
            media_count=stats_media_count,
            can_play=can_play,
            created_at=video.created_at,
            updated_at=video.updated_at,
            tags=cls._video_tags(video),
            persons=cls._video_persons(video),
            media_items=media_items,
        )

    @staticmethod
    def _require_tags(tag_ids: List[int]) -> None:
        for tag_id in tag_ids:
            if Tag.get_or_none(Tag.id == tag_id) is None:
                raise ApiError(404, "tag_not_found", "Tag not found", {"tag_id": tag_id})

    @staticmethod
    def _require_persons(person_ids: List[int]) -> None:
        for person_id in person_ids:
            if Person.get_or_none(Person.id == person_id) is None:
                raise ApiError(404, "person_not_found", "Person not found", {"person_id": person_id})

    @classmethod
    def _replace_tags(cls, video: VideoItem, tag_ids: List[int]) -> None:
        cls._require_tags(tag_ids)
        VideoItemTag.delete().where(VideoItemTag.video_item == video).execute()
        for tag_id in dict.fromkeys(tag_ids):
            VideoItemTag.create(video_item=video, tag=tag_id)

    @classmethod
    def _replace_persons(cls, video: VideoItem, person_ids: List[int]) -> None:
        cls._require_persons(person_ids)
        VideoItemPerson.delete().where(VideoItemPerson.video_item == video).execute()
        for person_id in dict.fromkeys(person_ids):
            VideoItemPerson.create(video_item=video, person=person_id)

    @classmethod
    def create_video(cls, payload: VideoItemCreateRequest) -> VideoItemDetailResource:
        with get_database().atomic():
            video = VideoItem.create(
                title=payload.title,
                summary=payload.summary,
                release_date=payload.release_date,
            )
            if payload.tag_ids:
                cls._replace_tags(video, payload.tag_ids)
            if payload.person_ids:
                cls._replace_persons(video, payload.person_ids)
        return cls.get_video_detail(video.id)

    @classmethod
    def update_video(cls, video_id: int, payload: VideoItemUpdateRequest) -> VideoItemDetailResource:
        video = cls._require_video(video_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(422, "validation_error", "At least one field must be provided")
        with get_database().atomic():
            if "title" in update_data and update_data["title"] is not None:
                video.title = update_data["title"]
            if "summary" in update_data and update_data["summary"] is not None:
                video.summary = update_data["summary"]
            if "release_date" in update_data:
                video.release_date = update_data["release_date"]
            video.updated_at = cls._current_time()
            video.save()
            # tag_ids/person_ids 传入即整体替换关联关系。
            if update_data.get("tag_ids") is not None:
                cls._replace_tags(video, update_data["tag_ids"])
            if update_data.get("person_ids") is not None:
                cls._replace_persons(video, update_data["person_ids"])
        return cls.get_video_detail(video.id)

    @classmethod
    def delete_video(cls, video_id: int) -> None:
        # 延迟导入避免与 media_service 顶层依赖链形成循环。
        from src.service.playback.media_service import MediaService

        video = cls._require_video(video_id)
        # 复用媒体删除链路：清理文件、缩略图图片、向量与级联子表，而非简单置空可空外键。
        media_ids = [media.id for media in Media.select(Media.id).where(Media.video_item == video)]
        for media_id in media_ids:
            MediaService.delete_media(media_id)
        # 剩余的标签/人物/合集关联均为非空外键，recursive 删除即可清理。
        video.delete_instance(recursive=True)
