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

    @classmethod
    def from_model(cls, library) -> "MediaLibraryResource":
        """把内部 backend_config 转成可公开配置，绝不序列化认证凭据。"""
        config = library.backend_config or {}
        if library.backend == MediaLibraryBackend.CLOUD115.value:
            public_config = {
                key: config[key]
                for key in ("root_cid", "app")
                if key in config
            }
        else:
            public_config = dict(config)
        return cls.model_validate(
            {
                "id": library.id,
                "name": library.name,
                "backend": library.backend,
                "backend_config": public_config,
                "created_at": library.created_at,
                "updated_at": library.updated_at,
            }
        )


class MediaLibraryCreateRequest(SchemaModel):
    name: str
    backend: MediaLibraryBackend
    backend_config: dict[str, Any]


class MediaLibraryUpdateRequest(SchemaModel):
    name: str | None = None
