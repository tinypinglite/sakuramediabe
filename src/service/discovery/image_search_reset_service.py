from src.api.exception.errors import ApiError
from src.model import ImageSearchSession, Media, MediaThumbnail, MoviePlotImage
from src.model.base import get_database
from src.service.discovery.embedding_client import (
    EmbeddingClientError,
    get_embedding_client,
)
from src.service.discovery.image_search_index_space_service import (
    ImageSearchIndexSpaceService,
)
from src.service.discovery.qdrant_plot_image_store import get_qdrant_plot_image_store
from src.service.discovery.qdrant_thumbnail_store import get_qdrant_thumbnail_store
from src.service.system.task_queue_service import (
    TaskQueueConflictError,
    TaskQueueService,
)


class ImageSearchResetService:
    @classmethod
    def reset(cls) -> dict[str, int]:
        # 先确认新配置的嵌入服务可用，避免把旧索引清空后才发现地址或认证配置错误。
        try:
            space = get_embedding_client().describe()
        except EmbeddingClientError as exc:
            raise ApiError(exc.status_code, exc.error_code, exc.message) from exc
        try:
            with get_database().atomic():
                TaskQueueService.enqueue(
                    task_key="image_search_index", trigger_type="manual", conflict="raise"
                )
                get_qdrant_thumbnail_store().clear()
                get_qdrant_plot_image_store().clear()
                sessions_deleted = ImageSearchSession.delete().execute()
                thumbnails_reset = (
                    MediaThumbnail.update(
                        image_search_index_status=MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
                    )
                    .from_(Media)
                    .where(MediaThumbnail.media == Media.id, Media.movie.is_null(False))
                    .execute()
                )
                plot_images_reset = MoviePlotImage.update(
                    image_search_index_status=MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_PENDING
                ).execute()
                ImageSearchIndexSpaceService.set_indexed_space(space.space_id)
        except TaskQueueConflictError as exc:
            raise ApiError(
                409,
                "image_search_reset_conflict",
                "Image search indexing is already running or queued",
                {"task_key": exc.task_key, "blocking_task_run_id": exc.blocking_task_run_id},
            ) from exc
        return {
            "sessions_deleted": int(sessions_deleted),
            "thumbnails_reset": int(thumbnails_reset),
            "plot_images_reset": int(plot_images_reset),
        }
