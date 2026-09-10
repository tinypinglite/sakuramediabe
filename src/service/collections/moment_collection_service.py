from peewee import IntegrityError, fn

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import require_by_id, validate_page
from src.model import (
    Image,
    Media,
    MediaPoint,
    MediaThumbnail,
    MomentCollection,
    MomentCollectionItem,
)
from src.model.base import get_database
from src.schema.catalog.actors import ImageResource
from src.schema.collections.moments import (
    MomentCollectionCreateRequest,
    MomentCollectionPointItemResource,
    MomentCollectionResource,
    MomentCollectionSummary,
    MomentCollectionUpdateRequest,
)
from src.schema.common.pagination import PageResponse


class MomentCollectionService:
    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ApiError(
                422, "validation_error", "Moment collection name cannot be empty"
            )
        return normalized

    @staticmethod
    def _normalize_description(description: str | None) -> str:
        return "" if description is None else description.strip()

    @staticmethod
    def _require_collection(collection_id: int) -> MomentCollection:
        return require_by_id(
            MomentCollection,
            collection_id,
            "moment_collection",
            error_message="Moment collection not found",
            error_details_key="collection_id",
        )

    @staticmethod
    def _require_point(point_id: int) -> MediaPoint:
        return require_by_id(
            MediaPoint,
            point_id,
            "media_point",
            error_message="Media point not found",
            error_details_key="point_id",
        )

    @classmethod
    def _ensure_name_available(cls, name: str, exclude_id: int | None = None) -> None:
        query = MomentCollection.select().where(MomentCollection.name == name)
        if exclude_id is not None:
            query = query.where(MomentCollection.id != exclude_id)
        if query.exists():
            raise ApiError(
                409,
                "moment_collection_name_conflict",
                "Moment collection name already exists",
                {"name": name},
            )

    @staticmethod
    def _items_query(collection_ids: list[int]):
        if not collection_ids:
            return MomentCollectionItem.select().where(False)
        return (
            MomentCollectionItem.select(
                MomentCollectionItem,
                MediaPoint,
                Media,
                MediaThumbnail,
                Image,
            )
            .join(MediaPoint)
            .join(Media)
            .switch(MediaPoint)
            .join(MediaThumbnail)
            .join(Image)
            .where(MomentCollectionItem.collection.in_(collection_ids))
        )

    @classmethod
    def _collection_counts(cls, collection_ids: list[int]) -> dict[int, int]:
        if not collection_ids:
            return {}
        rows = (
            MomentCollectionItem.select(
                MomentCollectionItem.collection,
                fn.COUNT(MomentCollectionItem.id).alias("point_count"),
            )
            .where(MomentCollectionItem.collection.in_(collection_ids))
            .group_by(MomentCollectionItem.collection)
        )
        return {row.collection_id: int(row.point_count) for row in rows}

    @classmethod
    def _collection_covers(cls, collection_ids: list[int]) -> dict[int, ImageResource]:
        if not collection_ids:
            return {}
        items = (
            MomentCollectionItem.select(
                MomentCollectionItem,
                MediaPoint,
                MediaThumbnail,
                Image,
            )
            .join(MediaPoint)
            .switch(MediaPoint)
            .join(MediaThumbnail)
            .join(Image)
            .where(MomentCollectionItem.collection.in_(collection_ids))
            .distinct(MomentCollectionItem.collection)
            .order_by(
                MomentCollectionItem.collection,
                MomentCollectionItem.position,
                MomentCollectionItem.id,
            )
        )
        return {
            item.collection_id: ImageResource.from_attributes_model(
                item.point.thumbnail.image
            )
            for item in items
        }

    @classmethod
    def _to_resource(
        cls,
        collection: MomentCollection,
        point_count: int,
        cover_image: ImageResource | None = None,
    ) -> MomentCollectionResource:
        return MomentCollectionResource(
            id=collection.id,
            name=collection.name,
            description=collection.description,
            point_count=point_count,
            cover_image=cover_image if point_count else None,
            created_at=collection.created_at,
            updated_at=collection.updated_at,
        )

    @classmethod
    def list_collections(cls) -> list[MomentCollectionResource]:
        collections = list(
            MomentCollection.select().order_by(
                MomentCollection.updated_at.desc(), MomentCollection.id.desc()
            )
        )
        counts = cls._collection_counts([collection.id for collection in collections])
        covers = cls._collection_covers([collection.id for collection in collections])
        return [
            cls._to_resource(
                collection,
                counts.get(collection.id, 0),
                covers.get(collection.id),
            )
            for collection in collections
        ]

    @classmethod
    def create_collection(
        cls, payload: MomentCollectionCreateRequest
    ) -> MomentCollectionResource:
        name = cls._normalize_name(payload.name)
        cls._ensure_name_available(name)
        collection = MomentCollection.create(
            name=name,
            description=cls._normalize_description(payload.description),
        )
        return cls._to_resource(collection, 0)

    @classmethod
    def get_collection(cls, collection_id: int) -> MomentCollectionResource:
        collection = cls._require_collection(collection_id)
        counts = cls._collection_counts([collection.id])
        covers = cls._collection_covers([collection.id])
        return cls._to_resource(
            collection, counts.get(collection.id, 0), covers.get(collection.id)
        )

    @classmethod
    def update_collection(
        cls,
        collection_id: int,
        payload: MomentCollectionUpdateRequest,
    ) -> MomentCollectionResource:
        collection = cls._require_collection(collection_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(
                422, "validation_error", "At least one field must be provided"
            )
        if "name" in update_data:
            name = cls._normalize_name(update_data["name"])
            if name != collection.name:
                cls._ensure_name_available(name, exclude_id=collection.id)
            collection.name = name
        if "description" in update_data:
            collection.description = cls._normalize_description(
                update_data["description"]
            )
        collection.updated_at = utc_now_for_db()
        collection.save()
        return cls.get_collection(collection.id)

    @classmethod
    def delete_collection(cls, collection_id: int) -> None:
        cls._require_collection(collection_id).delete_instance()

    @classmethod
    def list_collection_points(
        cls,
        collection_id: int,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MomentCollectionPointItemResource]:
        cls._require_collection(collection_id)
        validate_page(page, page_size, error_code="invalid_moment_collection_filter")
        total = (
            MomentCollectionItem.select()
            .where(MomentCollectionItem.collection == collection_id)
            .count()
        )
        start = (page - 1) * page_size
        items = list(
            cls._items_query([collection_id])
            .order_by(MomentCollectionItem.position, MomentCollectionItem.id)
            .offset(start)
            .limit(page_size)
        )
        resources = [
            MomentCollectionPointItemResource(
                point_id=item.point_id,
                media_id=item.point.media_id,
                movie_number=item.point.media.movie_number,
                video_item_id=item.point.media.video_item_id,
                thumbnail_id=item.point.thumbnail_id,
                offset_seconds=item.point.offset_seconds,
                image=ImageResource.from_attributes_model(item.point.thumbnail.image),
                created_at=item.point.created_at,
                position=item.position,
            )
            for item in items
        ]
        return PageResponse[MomentCollectionPointItemResource](
            items=resources,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def add_point(cls, collection_id: int, point_id: int) -> None:
        collection = cls._require_collection(collection_id)
        point = cls._require_point(point_id)
        if (
            MomentCollectionItem.get_or_none(
                MomentCollectionItem.collection == collection,
                MomentCollectionItem.point == point,
            )
            is not None
        ):
            return
        touched_at = utc_now_for_db()
        try:
            with get_database().atomic():
                next_position = (
                    MomentCollectionItem.select(
                        fn.COALESCE(fn.MAX(MomentCollectionItem.position), -1)
                    )
                    .where(MomentCollectionItem.collection == collection)
                    .scalar()
                ) + 1
                MomentCollectionItem.create(
                    collection=collection,
                    point=point,
                    position=next_position,
                    created_at=touched_at,
                    updated_at=touched_at,
                )
                collection.updated_at = touched_at
                collection.save(only=[MomentCollection.updated_at])
        except IntegrityError:
            return

    @classmethod
    def remove_point(cls, collection_id: int, point_id: int) -> None:
        collection = cls._require_collection(collection_id)
        deleted = (
            MomentCollectionItem.delete()
            .where(
                MomentCollectionItem.collection == collection,
                MomentCollectionItem.point == point_id,
            )
            .execute()
        )
        if deleted:
            collection.updated_at = utc_now_for_db()
            collection.save(only=[MomentCollection.updated_at])

    @classmethod
    def set_points(cls, collection_id: int, point_ids: list[int]) -> None:
        collection = cls._require_collection(collection_id)
        ordered_ids: list[int] = []
        seen: set[int] = set()
        for point_id in point_ids:
            if point_id not in seen:
                seen.add(point_id)
                ordered_ids.append(point_id)
        for point_id in ordered_ids:
            cls._require_point(point_id)
        touched_at = utc_now_for_db()
        with get_database().atomic():
            MomentCollectionItem.delete().where(
                MomentCollectionItem.collection == collection
            ).execute()
            for position, point_id in enumerate(ordered_ids):
                MomentCollectionItem.create(
                    collection=collection,
                    point=point_id,
                    position=position,
                    created_at=touched_at,
                    updated_at=touched_at,
                )
            collection.updated_at = touched_at
            collection.save(only=[MomentCollection.updated_at])

    @classmethod
    def list_point_collections(cls, point_id: int) -> list[MomentCollectionSummary]:
        cls._require_point(point_id)
        return [
            MomentCollectionSummary(id=item.collection_id, name=item.collection.name)
            for item in (
                MomentCollectionItem.select(MomentCollectionItem, MomentCollection)
                .join(MomentCollection)
                .where(MomentCollectionItem.point == point_id)
                .order_by(
                    MomentCollection.updated_at.desc(), MomentCollection.id.desc()
                )
            )
        ]
