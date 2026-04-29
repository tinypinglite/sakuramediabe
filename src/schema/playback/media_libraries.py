from datetime import datetime

from src.schema.common.base import SchemaModel


class MediaLibraryResource(SchemaModel):
    id: int
    name: str
    root_path: str
    created_at: datetime
    updated_at: datetime


class MediaLibraryCreateRequest(SchemaModel):
    name: str
    root_path: str


class MediaLibraryUpdateRequest(SchemaModel):
    name: str | None = None
    root_path: str | None = None
