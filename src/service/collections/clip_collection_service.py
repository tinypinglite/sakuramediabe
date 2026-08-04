"""片段合集 service。

负责片段合集（跨影片、有序、可连续播放）的增删改查与成员维护。
成员资源构建复用 ``MediaClipService``，封面/串流 URL 与片段接口保持一致。
阅读入口建议从 ``list_collections``、``list_collection_clips``、``set_clips`` 开始。
"""

from datetime import datetime

from peewee import IntegrityError, fn

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import count_by_owner, require_by_id, validate_page
from src.model import ClipCollection, ClipCollectionItem, MediaClip
from src.model.base import get_database
from src.schema.collections.clips import (
    ClipCollectionClipItemResource,
    ClipCollectionCreateRequest,
    ClipCollectionResource,
    ClipCollectionUpdateRequest,
)
from src.schema.common.pagination import PageResponse
from src.service.playback.media_clip_service import MediaClipService


class ClipCollectionService:
    @staticmethod
    def _current_time() -> datetime:
        return utc_now_for_db()

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ApiError(422, "validation_error", "Clip collection name cannot be empty")
        return normalized

    @staticmethod
    def _normalize_description(description: str | None) -> str:
        if description is None:
            return ""
        return description.strip()

    @classmethod
    def _ensure_name_available(cls, name: str, exclude_id: int | None = None) -> None:
        query = ClipCollection.select().where(ClipCollection.name == name)
        if exclude_id is not None:
            query = query.where(ClipCollection.id != exclude_id)
        if query.exists():
            raise ApiError(
                409,
                "clip_collection_name_conflict",
                "Clip collection name already exists",
                {"name": name},
            )

    @staticmethod
    def _require_collection(collection_id: int) -> ClipCollection:
        return require_by_id(
            ClipCollection,
            collection_id,
            "clip_collection",
            error_message="Clip collection not found",
            error_details_key="collection_id",
        )

    @staticmethod
    def _require_clip(clip_id: int) -> MediaClip:
        return require_by_id(MediaClip, clip_id, "media_clip", error_message="Media clip not found", error_details_key="clip_id")

    @staticmethod
    def _collection_counts(collection_ids: list[int]) -> dict[int, int]:
        return count_by_owner(ClipCollectionItem, ClipCollectionItem.collection, collection_ids)

    @classmethod
    def _collection_cover(cls, collection_id: int):
        """合集封面取按 position 排在最前的片段的封面。"""
        first_item = (
            ClipCollectionItem.select(ClipCollectionItem, MediaClip)
            .join(MediaClip)
            .where(ClipCollectionItem.collection == collection_id)
            .order_by(ClipCollectionItem.position.asc(), ClipCollectionItem.id.asc())
            .first()
        )
        if first_item is None:
            return None
        return MediaClipService.load_cover_map([first_item.clip]).get(
            (first_item.clip.media_id, first_item.clip.start_offset_seconds)
        )

    @classmethod
    def _to_resource(cls, collection: ClipCollection, clip_count: int) -> ClipCollectionResource:
        return ClipCollectionResource(
            id=collection.id,
            name=collection.name,
            description=collection.description,
            clip_count=clip_count,
            cover_image=cls._collection_cover(collection.id) if clip_count else None,
            created_at=collection.created_at,
            updated_at=collection.updated_at,
        )

    @classmethod
    def list_collections(cls) -> list[ClipCollectionResource]:
        collections = list(
            ClipCollection.select().order_by(
                ClipCollection.updated_at.desc(), ClipCollection.id.desc()
            )
        )
        counts = cls._collection_counts([collection.id for collection in collections])
        return [cls._to_resource(collection, counts.get(collection.id, 0)) for collection in collections]

    @classmethod
    def create_collection(cls, payload: ClipCollectionCreateRequest) -> ClipCollectionResource:
        name = cls._normalize_name(payload.name)
        description = cls._normalize_description(payload.description)
        cls._ensure_name_available(name)
        collection = ClipCollection.create(name=name, description=description)
        return cls._to_resource(collection, 0)

    @classmethod
    def get_collection(cls, collection_id: int) -> ClipCollectionResource:
        collection = cls._require_collection(collection_id)
        counts = cls._collection_counts([collection.id])
        return cls._to_resource(collection, counts.get(collection.id, 0))

    @classmethod
    def update_collection(
        cls, collection_id: int, payload: ClipCollectionUpdateRequest
    ) -> ClipCollectionResource:
        collection = cls._require_collection(collection_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(422, "validation_error", "At least one field must be provided")

        if "name" in update_data:
            name = cls._normalize_name(update_data["name"])
            if name != collection.name:
                cls._ensure_name_available(name, exclude_id=collection.id)
            collection.name = name
        if "description" in update_data:
            collection.description = cls._normalize_description(update_data["description"])

        collection.updated_at = cls._current_time()
        collection.save()
        counts = cls._collection_counts([collection.id])
        return cls._to_resource(collection, counts.get(collection.id, 0))

    @classmethod
    def delete_collection(cls, collection_id: int) -> None:
        collection = cls._require_collection(collection_id)
        # 仅删合集，成员关系 ClipCollectionItem 由外键 CASCADE 清理，不动片段本体。
        collection.delete_instance()

    @classmethod
    def list_collection_clips(
        cls,
        collection_id: int,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[ClipCollectionClipItemResource]:
        cls._require_collection(collection_id)
        validate_page(page, page_size, error_code="invalid_clip_collection_filter")
        total = ClipCollectionItem.select().where(ClipCollectionItem.collection == collection_id).count()
        items = list(
            ClipCollectionItem.select(ClipCollectionItem, MediaClip)
            .join(MediaClip)
            .where(ClipCollectionItem.collection == collection_id)
            .order_by(ClipCollectionItem.position.asc(), ClipCollectionItem.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        clips = [item.clip for item in items]
        cover_map = MediaClipService.load_cover_map(clips)
        resources = [
            ClipCollectionClipItemResource(
                **MediaClipService.clip_resource_fields(
                    item.clip,
                    cover_map.get((item.clip.media_id, item.clip.start_offset_seconds)),
                ),
                position=item.position,
            )
            for item in items
        ]
        return PageResponse[ClipCollectionClipItemResource](
            items=resources,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def add_clip(cls, collection_id: int, clip_id: int) -> None:
        collection = cls._require_collection(collection_id)
        clip = cls._require_clip(clip_id)
        existing = ClipCollectionItem.get_or_none(
            ClipCollectionItem.collection == collection,
            ClipCollectionItem.clip == clip,
        )
        if existing is not None:
            return
        touched_at = cls._current_time()
        try:
            with get_database().atomic():
                next_position = (
                    ClipCollectionItem.select(fn.COALESCE(fn.MAX(ClipCollectionItem.position), -1))
                    .where(ClipCollectionItem.collection == collection)
                    .scalar()
                ) + 1
                ClipCollectionItem.create(
                    collection=collection,
                    clip=clip,
                    position=next_position,
                    created_at=touched_at,
                    updated_at=touched_at,
                )
                collection.updated_at = touched_at
                collection.save(only=[ClipCollection.updated_at])
        except IntegrityError:
            # 与上方去重判断并发：该片段已被加入（唯一约束 (collection, clip) 命中），幂等返回。
            return

    @classmethod
    def remove_clip(cls, collection_id: int, clip_id: int) -> None:
        collection = cls._require_collection(collection_id)
        deleted = (
            ClipCollectionItem.delete()
            .where(
                ClipCollectionItem.collection == collection,
                ClipCollectionItem.clip == clip_id,
            )
            .execute()
        )
        if deleted:
            collection.updated_at = cls._current_time()
            collection.save(only=[ClipCollection.updated_at])

    @classmethod
    def set_clips(cls, collection_id: int, clip_ids: list[int]) -> None:
        """幂等地把合集成员设置为给定有序列表，既覆盖重排也覆盖批量设置成员。"""
        collection = cls._require_collection(collection_id)
        # 去重保序：同一片段在合集内只出现一次，以首次出现的位置为准。
        ordered_ids: list[int] = []
        seen: set[int] = set()
        for clip_id in clip_ids:
            if clip_id not in seen:
                seen.add(clip_id)
                ordered_ids.append(clip_id)
        for clip_id in ordered_ids:
            cls._require_clip(clip_id)

        touched_at = cls._current_time()
        with get_database().atomic():
            ClipCollectionItem.delete().where(
                ClipCollectionItem.collection == collection
            ).execute()
            for position, clip_id in enumerate(ordered_ids):
                ClipCollectionItem.create(
                    collection=collection,
                    clip=clip_id,
                    position=position,
                    created_at=touched_at,
                    updated_at=touched_at,
                )
            collection.updated_at = touched_at
            collection.save(only=[ClipCollection.updated_at])
