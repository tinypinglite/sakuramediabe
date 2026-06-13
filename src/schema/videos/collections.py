from datetime import datetime
from typing import List

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel
from src.schema.videos.items import VideoItemListItemResource


class VideoCollectionResource(SchemaModel):
    id: int
    name: str
    description: str = ""
    item_count: int = 0
    # 合集封面取按顺序排在最前的视频封面；空合集或来源缺失时为空。
    cover_image: ImageResource | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_collection(
        cls, collection, item_count: int = 0, cover_image: ImageResource | None = None
    ) -> "VideoCollectionResource":
        return cls.from_peewee_model(
            collection, extra={"item_count": item_count, "cover_image": cover_image}
        )


class VideoCollectionItemResource(SchemaModel):
    # 合集成员，position 决定顺序播放次序。
    item_id: int
    position: int
    video: VideoItemListItemResource


class VideoCollectionCreateRequest(SchemaModel):
    name: str = Field(min_length=1)
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized


class VideoCollectionUpdateRequest(SchemaModel):
    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized


class VideoCollectionItemAddRequest(SchemaModel):
    video_item_id: int = Field(gt=0)


class VideoCollectionReorderRequest(SchemaModel):
    # 按目标顺序给出合集成员 item_id 列表，service 据此重写 position。
    ordered_item_ids: List[int] = Field(min_length=1)
