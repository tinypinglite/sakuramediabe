from src.api.exception.errors import ApiError
from src.service.system.task_queue_service import (
    TaskQueueConflictError,
    TaskQueueService,
)


class ImageSearchResetService:
    @classmethod
    def reset(cls) -> dict[str, int]:
        try:
            task_run = TaskQueueService.enqueue(
                task_key="image_search_index",
                trigger_type="manual",
                params={"reset": True},
                conflict="raise",
            )
        except TaskQueueConflictError as exc:
            raise ApiError(
                409,
                "image_search_reset_conflict",
                "Image search indexing is already running or queued",
                {"task_key": exc.task_key, "blocking_task_run_id": exc.blocking_task_run_id},
            ) from exc
        return {"task_run_id": int(task_run.id)}
