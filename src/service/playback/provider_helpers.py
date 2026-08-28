from copy import deepcopy

from src.model import Media, MediaLibrary
from src.plugins.provider_protocol import LibraryHandle, MediaHandle


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
    )
