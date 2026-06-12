from datetime import datetime
from typing import List

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import MovieMediaResource, TagResource
from src.schema.common.base import SchemaModel


class VideoPersonResource(SchemaModel):
    # 视频详情里内嵌的人物精简结构。
    id: int
    name: str
    avatar_image: ImageResource | None = None


class VideoItemListItemResource(SchemaModel):
    id: int
    title: str
    summary: str = ""
    cover_image: ImageResource | None = None
    release_date: datetime | None = None
    media_count: int = 0
    can_play: bool = False
    created_at: datetime
    updated_at: datetime


class VideoItemDetailResource(VideoItemListItemResource):
    tags: List[TagResource] = Field(default_factory=list)
    persons: List[VideoPersonResource] = Field(default_factory=list)
    media_items: List[MovieMediaResource] = Field(default_factory=list)


class VideoItemCreateRequest(SchemaModel):
    title: str = Field(min_length=1)
    summary: str = ""
    release_date: datetime | None = None
    tag_ids: List[int] = Field(default_factory=list)
    person_ids: List[int] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title cannot be blank")
        return normalized


class VideoItemUpdateRequest(SchemaModel):
    title: str | None = None
    summary: str | None = None
    release_date: datetime | None = None
    # 传入则整体替换关联关系；不传（None）则保持原值。
    tag_ids: List[int] | None = None
    person_ids: List[int] | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("title cannot be blank")
        return normalized
