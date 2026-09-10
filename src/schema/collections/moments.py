from datetime import datetime

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel
from src.schema.playback.media import MediaPointListItemResource


class MomentCollectionCreateRequest(SchemaModel):
    name: str = Field(min_length=1)
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized


class MomentCollectionUpdateRequest(SchemaModel):
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


class MomentCollectionResource(SchemaModel):
    id: int
    name: str
    description: str = ""
    point_count: int = 0
    cover_image: ImageResource | None = None
    created_at: datetime
    updated_at: datetime


class MomentCollectionPointItemResource(MediaPointListItemResource):
    position: int


class MomentCollectionSetPointsRequest(SchemaModel):
    point_ids: list[int]


class MomentCollectionSummary(SchemaModel):
    id: int
    name: str
