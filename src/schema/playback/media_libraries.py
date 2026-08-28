from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from src.schema.common.base import SchemaModel


class MediaProviderConfigFieldResource(SchemaModel):
    key: str
    label: str
    input: Literal["text", "secret", "path"]
    required: bool
    description: str | None = None
    multiline: bool = False
    read_only: bool = False
    hint: str | None = None


class MediaLibraryProviderResource(SchemaModel):
    provider_key: str
    display_name: str
    library_config_fields: list[MediaProviderConfigFieldResource] = Field(default_factory=list)
    playback_deliveries: list[Literal["proxy", "redirect"]] = Field(default_factory=list)
    download_config_fields: list[MediaProviderConfigFieldResource] | None


class MediaLibraryResource(SchemaModel):
    id: int
    name: str
    provider_key: str
    provider_config: dict[str, Any]
    account_key: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, library) -> "MediaLibraryResource":
        return cls.model_validate(
            {
                "id": library.id,
                "name": library.name,
                "provider_key": library.provider_key,
                "provider_config": library.provider_config or {},
                "account_key": library.account_key,
                "created_at": library.created_at,
                "updated_at": library.updated_at,
            }
        )


class MediaLibraryCreateRequest(SchemaModel):
    name: str
    provider_key: str
    provider_config: dict[str, Any] = Field(default_factory=dict)


class MediaLibraryUpdateRequest(SchemaModel):
    name: str | None = None
    provider_config: dict[str, Any] | None = None
