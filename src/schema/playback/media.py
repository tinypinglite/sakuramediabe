from datetime import datetime
from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel


class MediaProgressUpdateRequest(SchemaModel):
    position_seconds: int = Field(ge=0)

    @field_validator("position_seconds")
    @classmethod
    def validate_position_seconds(cls, value: int) -> int:
        if value < 0:
            raise ValueError("position_seconds cannot be negative")
        return value


class MediaProgressResource(SchemaModel):
    media_id: int
    last_position_seconds: int
    last_watched_at: datetime


class MediaPointCreateRequest(SchemaModel):
    thumbnail_id: int = Field(gt=0)

    @field_validator("thumbnail_id")
    @classmethod
    def validate_thumbnail_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("thumbnail_id must be greater than 0")
        return value


class MediaPointResource(SchemaModel):
    point_id: int
    media_id: int
    thumbnail_id: int
    offset_seconds: int
    image: ImageResource
    created_at: datetime


class MediaPointListItemResource(SchemaModel):
    point_id: int
    media_id: int
    movie_number: str
    thumbnail_id: int
    offset_seconds: int
    image: ImageResource
    created_at: datetime


class MediaThumbnailResource(SchemaModel):
    thumbnail_id: int
    media_id: int
    offset_seconds: int
    image: ImageResource
