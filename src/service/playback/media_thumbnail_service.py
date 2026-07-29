"""媒体缩略图兼容门面；实现已拆至 ``playback.thumbnails``。"""

from pathlib import Path

from PIL import Image as PILImage

from src.common.media_paths import media_image_root_path
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.backends.cloud115_hls import (
    Cloud115HlsThumbnailBackend,
)
from src.service.playback.thumbnails.backends.local import LocalThumbnailBackend
from src.service.playback.thumbnails.contracts import PreparedThumbnailSource
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService


class MediaThumbnailService:
    TASK_KEY = MediaThumbnailTaskService.TASK_KEY
    THUMBNAIL_MAX_RETRIES = MediaThumbnailTaskService.THUMBNAIL_MAX_RETRIES
    INTERRUPTED_GENERATION_ERROR_MESSAGE = (
        MediaThumbnailTaskService.INTERRUPTED_GENERATION_ERROR_MESSAGE
    )
    CLOUD115_THUMBNAIL_UA = Cloud115HlsThumbnailBackend.USER_AGENT
    CLOUD115_HLS_SEGMENT_MAX_WORKERS = Cloud115HlsThumbnailBackend.SEGMENT_MAX_WORKERS
    CLOUD115_HLS_STREAM_CHUNK_SIZE = Cloud115HlsThumbnailBackend.STREAM_CHUNK_SIZE
    CLOUD115_HLS_PROBE_SIZE = Cloud115HlsThumbnailBackend.PROBE_SIZE
    CLOUD115_SYSTEM_FAILURES = Cloud115HlsThumbnailBackend.SYSTEM_FAILURES

    count_pending_media = MediaThumbnailTaskService.count_pending_media
    recover_interrupted_running_media = (
        MediaThumbnailTaskService.recover_interrupted_running_media
    )
    generate_pending_thumbnails = MediaThumbnailTaskService.generate_pending_thumbnails
    list_media_thumbnails = ThumbnailArtifactService.list_media_thumbnails

    _minimum_acceptable_thumbnail_count = (
        MediaThumbnailTaskService.minimum_acceptable_count
    )
    _select_lowest_hls_definition = Cloud115HlsThumbnailBackend.select_lowest_definition
    _build_hls_thumbnail_targets = Cloud115HlsThumbnailBackend.build_targets
    _decode_hls_segment_to_webp = Cloud115HlsThumbnailBackend.decode_segment

    @classmethod
    def _generate_cloud115_hls_webp(
        cls, targets, webp_dir: Path, *, source_label: str
    ):
        prepared = PreparedThumbnailSource(
            source_label=source_label,
            expected_count=sum(len(offsets) for _, offsets in targets),
            payload=targets,
        )
        return Cloud115HlsThumbnailBackend.generate(prepared, webp_dir).first_error

    @staticmethod
    def _generate_webp_with_pyav(
        video_source,
        webp_dir: Path,
        *,
        interval_seconds: int = 10,
        source_label: str | None = None,
    ):
        prepared = PreparedThumbnailSource(
            source_label=source_label or str(video_source),
            expected_count=0,
            payload=video_source,
        )
        return LocalThumbnailBackend.generate(prepared, webp_dir).first_error

    @staticmethod
    def _image_root_path() -> Path:
        return media_image_root_path()

    @classmethod
    def _read_thumbnail_dimensions(cls, image_origin: str):
        # 保留旧测试替换点；新实现使用 ThumbnailArtifactService。
        with PILImage.open(cls._image_root_path() / image_origin) as image:
            return image.size
