"""媒体缩略图兼容门面；实现已拆至 ``playback.thumbnails``。"""

from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService


class MediaThumbnailService:
    TASK_KEY = MediaThumbnailTaskService.TASK_KEY
    INTERRUPTED_GENERATION_ERROR_MESSAGE = (
        MediaThumbnailTaskService.INTERRUPTED_GENERATION_ERROR_MESSAGE
    )

    count_pending_media = MediaThumbnailTaskService.count_pending_media
    recover_interrupted_running_media = (
        MediaThumbnailTaskService.recover_interrupted_running_media
    )
    generate_pending_thumbnails = MediaThumbnailTaskService.generate_pending_thumbnails
    list_media_thumbnails = ThumbnailArtifactService.list_media_thumbnails
