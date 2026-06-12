from datetime import datetime

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel


class PersonResource(SchemaModel):
    id: int
    name: str
    avatar_image: ImageResource | None = None
    gender: int = 0
    video_count: int = 0
    created_at: datetime
    updated_at: datetime


class PersonCreateRequest(SchemaModel):
    name: str = Field(min_length=1)
    gender: int = 0

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized


class PersonUpdateRequest(SchemaModel):
    name: str | None = None
    gender: int | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized
