from datetime import datetime
from typing import List

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import MovieMediaResource
from src.schema.common.base import SchemaModel


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
    media_items: List[MovieMediaResource] = Field(default_factory=list)


class VideoItemCreateRequest(SchemaModel):
    title: str = Field(min_length=1)
    summary: str = ""
    release_date: datetime | None = None

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

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("title cannot be blank")
        return normalized
