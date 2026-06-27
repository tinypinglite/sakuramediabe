"""视频合集（VideoCollection）service：合集增删改查、成员管理与顺序维护。"""

from datetime import datetime
from typing import Dict, List

from peewee import IntegrityError, JOIN, fn

from src.api.exception.errors import ApiError
from src.common import build_signed_media_url
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import require_record, validate_page
from src.model import Image, VideoCollection, VideoCollectionItem, VideoItem
from src.model.base import get_database
from src.schema.catalog.actors import ImageResource
from src.schema.common.pagination import PageResponse
from src.schema.videos.collections import (
    VideoCollectionCreateRequest,
    VideoCollectionItemResource,
    VideoCollectionResource,
    VideoCollectionUpdateRequest,
)
from src.service.videos.video_item_service import VideoItemService


class VideoCollectionService:
    @staticmethod
    def _current_time() -> datetime:
        return utc_now_for_db()

    @staticmethod
    def _require_collection(collection_id: int) -> VideoCollection:
        return require_record(
            VideoCollection,
            VideoCollection.id == collection_id,
            error_code="video_collection_not_found",
            error_message="Video collection not found",
            error_details={"collection_id": collection_id},
        )

    @staticmethod
    def _require_video(video_item_id: int) -> VideoItem:
        return require_record(
            VideoItem,
            VideoItem.id == video_item_id,
            error_code="video_item_not_found",
            error_message="Video item not found",
            error_details={"video_item_id": video_item_id},
        )

    @classmethod
    def _ensure_name_available(cls, name: str, exclude_id: int | None = None) -> None:
        query = VideoCollection.select().where(VideoCollection.name == name)
        if exclude_id is not None:
            query = query.where(VideoCollection.id != exclude_id)
        if query.exists():
            raise ApiError(
                409,
                "video_collection_name_conflict",
                "Video collection name already exists",
                {"name": name},
            )

    @staticmethod
    def _item_counts(collection_ids: List[int]) -> Dict[int, int]:
        if not collection_ids:
            return {}
        query = (
            VideoCollectionItem.select(
                VideoCollectionItem.collection,
                fn.COUNT(VideoCollectionItem.id).alias("item_count"),
            )
            .where(VideoCollectionItem.collection.in_(collection_ids))
            .group_by(VideoCollectionItem.collection)
        )
        return {row.collection_id: row.item_count for row in query}

    @staticmethod
    def _collection_cover(collection_id: int) -> ImageResource | None:
        """合集封面取按 position 排在最前的视频封面。"""
        first_item = (
            VideoCollectionItem.select(VideoCollectionItem, VideoItem, Image)
            .join(VideoItem)
            .join(Image, JOIN.LEFT_OUTER, on=(VideoItem.cover_image == Image.id))
            .where(VideoCollectionItem.collection == collection_id)
            .order_by(VideoCollectionItem.position.asc(), VideoCollectionItem.id.asc())
            .first()
        )
        if first_item is None or first_item.video_item.cover_image_id is None:
            return None
        return ImageResource.from_attributes_model(first_item.video_item.cover_image)

    @staticmethod
    def _touch_collection(collection: VideoCollection, touched_at: datetime) -> None:
        collection.updated_at = touched_at
        collection.save(only=[VideoCollection.updated_at])

    @classmethod
    def list_collections(cls) -> List[VideoCollectionResource]:
        collections = list(
            VideoCollection.select().order_by(
                VideoCollection.updated_at.desc(), VideoCollection.id.desc()
            )
        )
        counts = cls._item_counts([collection.id for collection in collections])
        return [
            VideoCollectionResource.from_collection(
                collection,
                item_count=counts.get(collection.id, 0),
                cover_image=cls._collection_cover(collection.id) if counts.get(collection.id, 0) else None,
            )
            for collection in collections
        ]

    @classmethod
    def get_collection(cls, collection_id: int) -> VideoCollectionResource:
        collection = cls._require_collection(collection_id)
        counts = cls._item_counts([collection.id])
        item_count = counts.get(collection.id, 0)
        return VideoCollectionResource.from_collection(
            collection,
            item_count=item_count,
            cover_image=cls._collection_cover(collection.id) if item_count else None,
        )

    @classmethod
    def create_collection(cls, payload: VideoCollectionCreateRequest) -> VideoCollectionResource:
        cls._ensure_name_available(payload.name)
        collection = VideoCollection.create(name=payload.name, description=payload.description)
        return VideoCollectionResource.from_collection(collection, item_count=0)

    @classmethod
    def update_collection(
        cls, collection_id: int, payload: VideoCollectionUpdateRequest
    ) -> VideoCollectionResource:
        collection = cls._require_collection(collection_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(422, "validation_error", "At least one field must be provided")
        if "name" in update_data and update_data["name"] is not None:
            name = update_data["name"]
            if name != collection.name:
                cls._ensure_name_available(name, exclude_id=collection.id)
            collection.name = name
        if "description" in update_data and update_data["description"] is not None:
            collection.description = update_data["description"]
        collection.updated_at = cls._current_time()
        collection.save()
        return cls.get_collection(collection.id)

    @classmethod
    def delete_collection(cls, collection_id: int) -> None:
        collection = cls._require_collection(collection_id)
        # 仅删合集，成员关联 VideoCollectionItem 由外键 CASCADE 清理，视频条目本身保留。
        collection.delete_instance()

    @classmethod
    def list_collection_items(
        cls,
        collection_id: int,
        sort: str | None = None,
        page: int = 1,
        page_size: int = 20,
        include_play_url: bool = False,
    ) -> PageResponse[VideoCollectionItemResource]:
        """分页拉取合集成员。万级成员合集不再一次性全返（避免连播页首请求超时）。

        [include_play_url] 为 True 时为每个成员内联「首个媒体」的签名播放地址，
        供连播页直接组装播放列表，免去逐集 getVideoDetail 的 N+1 风暴。
        """
        collection = cls._require_collection(collection_id)
        validate_page(page, page_size, error_code="invalid_video_filter")
        total = (
            VideoCollectionItem.select()
            .where(VideoCollectionItem.collection == collection)
            .count()
        )
        items = cls._query_item_resources(
            collection,
            sort=sort,
            include_play_url=include_play_url,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return PageResponse[VideoCollectionItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def _query_item_resources(
        cls,
        collection: VideoCollection,
        *,
        sort: str | None = None,
        include_play_url: bool = False,
        offset: int | None = None,
        limit: int | None = None,
    ) -> List[VideoCollectionItemResource]:
        """查询并组装成员资源列表。[offset]/[limit] 为 None 时返回全部（供 reorder 用）。"""
        first_media, first_media_id = VideoItemService._first_media_alias()
        # 默认 position 升序（合集手动顺序，前端据此顺序播放）；另支持入库时间/标题/时长/文件大小排序。
        order_by = VideoItemService._build_video_order(
            sort,
            first_media=first_media,
            default_sort="position:asc",
            extra_columns={"position": VideoCollectionItem.position},
            tie_breaker=VideoCollectionItem.id,
        )
        # 一次 LEFT JOIN 预加载封面与第一条媒体时长/大小，既供排序也供展示；
        # 同时取出第一条媒体的 id（first_media.id），用于按需生成签名播放地址。
        query = (
            VideoCollectionItem.select(
                VideoCollectionItem,
                VideoItem,
                Image,
                fn.COALESCE(first_media.duration_seconds, 0).alias("first_duration_seconds"),
                fn.COALESCE(first_media.file_size_bytes, 0).alias("first_file_size_bytes"),
                fn.COALESCE(first_media.resolution, "").alias("first_resolution"),
                # 用 COALESCE 包裹才会作为标量挂到 link 上（裸 alias 字段会归到 aliased model）；
                # 0 作「无媒体」哨兵（media id 恒为正）。
                fn.COALESCE(first_media.id, 0).alias("play_media_id"),
            )
            .join(VideoItem)
            .join(Image, JOIN.LEFT_OUTER, on=(VideoItem.cover_image == Image.id))
            .switch(VideoItem)
            .join(first_media_id, JOIN.LEFT_OUTER, on=(first_media_id.c.owner_id == VideoItem.id))
            .join(first_media, JOIN.LEFT_OUTER, on=(first_media.id == first_media_id.c.first_media_id))
            .where(VideoCollectionItem.collection == collection)
            .order_by(*order_by)
        )
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        links = list(query)
        stats = VideoItemService._media_stats([link.video_item_id for link in links])
        items: List[VideoCollectionItemResource] = []
        for link in links:
            media_count, can_play = stats.get(link.video_item_id, (0, False))
            play_url = (
                build_signed_media_url(link.play_media_id)
                if include_play_url and link.play_media_id
                else None
            )
            cover_width, cover_height = VideoItemService._parse_resolution(
                link.first_resolution
            )
            items.append(
                VideoCollectionItemResource(
                    item_id=link.id,
                    position=link.position,
                    play_url=play_url,
                    # 0 为「无媒体」哨兵，归一为 None。
                    first_media_id=link.play_media_id or None,
                    video=VideoItemService._to_list_item(
                        link.video_item,
                        media_count,
                        can_play,
                        duration_seconds=link.first_duration_seconds,
                        file_size_bytes=link.first_file_size_bytes,
                        cover_width=cover_width,
                        cover_height=cover_height,
                    ),
                )
            )
        return items

    @classmethod
    def _next_position(cls, collection: VideoCollection) -> int:
        current_max = (
            VideoCollectionItem.select(fn.MAX(VideoCollectionItem.position))
            .where(VideoCollectionItem.collection == collection)
            .scalar()
        )
        return (current_max if current_max is not None else -1) + 1

    @classmethod
    def add_item(cls, collection_id: int, video_item_id: int) -> None:
        collection = cls._require_collection(collection_id)
        video = cls._require_video(video_item_id)
        existing = VideoCollectionItem.get_or_none(
            VideoCollectionItem.collection == collection,
            VideoCollectionItem.video_item == video,
        )
        if existing is not None:
            return
        touched_at = cls._current_time()
        # 位置计算与插入纳入同一事务，避免并发追加时 _next_position 读到陈旧 MAX。
        try:
            with get_database().atomic():
                VideoCollectionItem.create(
                    collection=collection,
                    video_item=video,
                    position=cls._next_position(collection),
                    created_at=touched_at,
                    updated_at=touched_at,
                )
                cls._touch_collection(collection, touched_at)
        except IntegrityError:
            # 与上方去重判断并发：该视频已被加入（唯一约束 (collection, video_item) 命中），幂等返回。
            return

    @classmethod
    def remove_item(cls, collection_id: int, item_id: int) -> None:
        collection = cls._require_collection(collection_id)
        deleted = (
            VideoCollectionItem.delete()
            .where(
                VideoCollectionItem.id == item_id,
                VideoCollectionItem.collection == collection,
            )
            .execute()
        )
        if deleted:
            cls._touch_collection(collection, cls._current_time())

    @classmethod
    def reorder_items(cls, collection_id: int, ordered_item_ids: List[int]) -> List[VideoCollectionItemResource]:
        collection = cls._require_collection(collection_id)
        existing_ids = {
            link.id
            for link in VideoCollectionItem.select(VideoCollectionItem.id).where(
                VideoCollectionItem.collection == collection
            )
        }
        normalized = list(dict.fromkeys(ordered_item_ids))
        # 校验：给出的 item_id 必须恰好覆盖该合集的全部成员，避免漏排或越权。
        if set(normalized) != existing_ids:
            raise ApiError(
                422,
                "invalid_collection_reorder",
                "ordered_item_ids must cover exactly all collection items",
                {"collection_id": collection_id},
            )
        touched_at = cls._current_time()
        with get_database().atomic():
            for position, item_id in enumerate(normalized):
                item = VideoCollectionItem.get_by_id(item_id)
                item.position = position
                item.updated_at = touched_at
                item.save(only=[VideoCollectionItem.position, VideoCollectionItem.updated_at])
            cls._touch_collection(collection, touched_at)
        # 返回重排后的全部成员（position 升序，无需分页 / 播放地址）。
        return cls._query_item_resources(collection)
