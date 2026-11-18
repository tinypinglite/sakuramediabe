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


class MediaProviderLoginCookieCaptureResource(SchemaModel):
    url: str
    capture_names: list[str] = Field(default_factory=list)
    required_names: list[str] = Field(default_factory=list)


class MediaProviderLoginLocalStorageCaptureResource(SchemaModel):
    origin: str
    required_json_keys: list[str] = Field(default_factory=list)


class MediaProviderLibraryLoginResource(SchemaModel):
    mode: Literal["webview"]
    start_url: str
    allowed_hosts: list[str] = Field(default_factory=list)
    cookie_captures: list[MediaProviderLoginCookieCaptureResource] = Field(
        default_factory=list
    )
    local_storage_captures: list[
        MediaProviderLoginLocalStorageCaptureResource
    ] = Field(default_factory=list)
    managed_config_keys: list[str] = Field(default_factory=list)
    timeout_seconds: int


class MediaLibraryProviderResource(SchemaModel):
    provider_key: str
    display_name: str
    library_config_fields: list[MediaProviderConfigFieldResource] = Field(default_factory=list)
    library_login: MediaProviderLibraryLoginResource | None = None
    playback_deliveries: list[Literal["proxy", "redirect"]] = Field(default_factory=list)
    download_config_fields: list[MediaProviderConfigFieldResource] | None


class MediaLibraryResource(SchemaModel):
    id: int
    name: str
    provider_key: str
    provider_config: dict[str, Any]
    account_key: str | None = None
    supports_in_place_import: bool = False
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
    auth_session_id: str | None = None


class MediaLibraryUpdateRequest(SchemaModel):
    name: str | None = None
    provider_config: dict[str, Any] | None = None
    auth_session_id: str | None = None


class MediaLibraryLoginCookieSet(SchemaModel):
    url: str
    values: dict[str, str] = Field(default_factory=dict)


class MediaLibraryLoginLocalStorageSet(SchemaModel):
    origin: str
    items: dict[str, str] = Field(default_factory=dict)


class MediaLibraryProviderLoginSessionCreateRequest(SchemaModel):
    provider_key: str
    cookies: list[MediaLibraryLoginCookieSet] = Field(default_factory=list)
    local_storage: list[MediaLibraryLoginLocalStorageSet] = Field(default_factory=list)


class MediaLibraryProviderLoginSessionResource(SchemaModel):
    id: str
    provider_key: str
    account_label: str | None
    expires_at: datetime
