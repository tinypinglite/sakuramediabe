from src.model import Media
from src.model.enums import MediaLibraryBackend
from src.service.playback.thumbnails.backends.cloud115_hls import (
    Cloud115HlsThumbnailBackend,
)
from src.service.playback.thumbnails.backends.local import LocalThumbnailBackend


class ThumbnailBackendRegistry:
    _backends = {
        MediaLibraryBackend.LOCAL.value: LocalThumbnailBackend,
        MediaLibraryBackend.CLOUD115.value: Cloud115HlsThumbnailBackend,
    }

    @classmethod
    def for_media(cls, media: Media):
        library = media.library
        backend_key = library.backend if library is not None else None
        try:
            return cls._backends[backend_key]
        except KeyError as exc:
            raise RuntimeError(f"unsupported_thumbnail_backend:{backend_key}") from exc
