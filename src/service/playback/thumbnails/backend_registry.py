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
    def for_backend_key(cls, backend_key: str):
        try:
            return cls._backends[backend_key]
        except KeyError as exc:
            raise RuntimeError(f"unsupported_thumbnail_backend:{backend_key}") from exc

    @classmethod
    def for_media(cls, media: Media):
        library = media.library
        backend_key = library.backend if library is not None else None
        return cls.for_backend_key(backend_key)

    @classmethod
    def ensure_available(cls, backend_key: str) -> None:
        """在整条泳道开始前检查共享依赖，避免把环境故障扩散成逐媒体失败。"""
        ensure_available = getattr(cls.for_backend_key(backend_key), "ensure_available", None)
        if callable(ensure_available):
            ensure_available()
