from copy import deepcopy

from src.api.exception.errors import ApiError
from src.model import Media, MediaLibrary
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    LibraryHandle,
    MediaHandle,
    ProviderUnavailableError,
)


def require_media_provider(provider_key: str):
    try:
        return MEDIA_PROVIDER_REGISTRY.require(provider_key)
    except ProviderUnavailableError as exc:
        raise ApiError(
            503,
            "provider_not_installed",
            "媒体提供方未安装",
            {"provider_key": provider_key},
        ) from exc


def library_handle_for(library: MediaLibrary) -> LibraryHandle:
    return LibraryHandle(
        library_id=library.id,
        provider_key=library.provider_key,
        provider_config=deepcopy(library.provider_config or {}),
        account_key=library.account_key,
    )


def media_handle_for(media: Media) -> MediaHandle:
    return MediaHandle(
        media_id=media.id,
        library=library_handle_for(media.library),
        storage_ref=deepcopy(media.storage_ref or {}),
        file_name=media.file_name,
        file_size_bytes=media.file_size_bytes,
        duration_seconds=media.duration_seconds,
        content_md5=getattr(media, "content_md5", None),
        content_sha1=getattr(media, "content_sha1", None),
        content_gcid=getattr(media, "content_gcid", None),
    )
