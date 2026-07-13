from datetime import datetime
from typing import Any

from src.model.enums import MediaLibraryBackend
from src.schema.common.base import SchemaModel


class MediaLibraryResource(SchemaModel):
    id: int
    name: str
    backend: MediaLibraryBackend
    backend_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class MediaLibraryCreateRequest(SchemaModel):
    name: str
    backend: MediaLibraryBackend
    backend_config: dict[str, Any]


class MediaLibraryUpdateRequest(SchemaModel):
    name: str | None = None
