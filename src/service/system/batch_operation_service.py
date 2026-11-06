"""用户批量操作的队列任务执行器。

每笔提交直接创建一行 pending 任务（不占 mutex），由专属 ``batch`` 单并发道按提交
顺序串行领取执行；后来的批量操作自然排队，不存在并发写争抢。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.api.exception.errors import ApiError
from src.i18n import translate
from src.schema.system.batch_operations import (
    BatchOperationAcceptedResponse,
    BatchOperationRequest,
    BatchOperationType,
)
from src.service.system import ActivityService


class BatchOperationTaskService:
    TASK_KEY = "batch_operation"

    # 操作名的语言包 key 以枚举值为 slug，新增操作类型只需同步补语言包。
    OPERATION_LABEL_KEYS: dict[BatchOperationType, str] = {
        operation: f"tasks.batch_operation.operation.{operation.value}"
        for operation in BatchOperationType
    }

    @classmethod
    def operation_label(cls, operation: BatchOperationType) -> str:
        """按当前语言取操作名；任务名写时落库，之后不再重译。"""
        return translate(cls.OPERATION_LABEL_KEYS[operation])

    @classmethod
    def enqueue(cls, request: BatchOperationRequest) -> BatchOperationAcceptedResponse:
        task = ActivityService.create_task_run(
            task_key=cls.TASK_KEY,
            task_name=cls.operation_label(request.operation),
            trigger_type="manual",
            params=request.model_dump(mode="json"),
        )
        return BatchOperationAcceptedResponse(
            task_run_id=task.id,
            task_key=task.task_key,
            state=task.state,
        )

    @classmethod
    def execute(cls, reporter, params: dict[str, Any]) -> dict[str, Any]:
        """逐项执行；单条失败只记入 failed 清单，不中断剩余项。"""
        request = BatchOperationRequest.model_validate(params)
        action = cls._build_action(request)
        label = cls.operation_label(request.operation)
        failed: list[dict[str, Any]] = []
        succeeded = 0
        total = len(request.ids)
        for index, item_id in enumerate(request.ids, start=1):
            try:
                action(item_id)
                succeeded += 1
            except Exception as exc:
                failed.append({"id": item_id, "message": cls._failure_message(exc)})
            reporter.emit(
                current=index,
                total=total,
                text=translate(
                    "tasks.batch_operation.progress.running",
                    operation=label,
                    current=index,
                    total=total,
                ),
            )
        return {
            "operation": request.operation.value,
            "total": total,
            "succeeded_count": succeeded,
            "failed_count": len(failed),
            "failed": failed,
        }

    @classmethod
    def _build_action(cls, request: BatchOperationRequest) -> Callable[[int], None]:
        operation = request.operation
        if operation is BatchOperationType.MEDIA_DELETE:
            from src.service.playback.media_service import MediaService

            excluded_media_ids = frozenset(request.ids)
            return lambda media_id: MediaService.delete_media(
                media_id,
                migration_excluded_media_ids=excluded_media_ids,
            )
        if operation is BatchOperationType.CLIP_DELETE:
            from src.service.playback.media_clip_service import MediaClipService

            return MediaClipService.delete_clip
        if operation is BatchOperationType.MEDIA_POINT_DELETE:
            from src.service.playback.media_service import MediaService

            return MediaService.delete_point_by_id
        if operation is BatchOperationType.VIDEO_DELETE:
            from src.service.videos.video_item_service import VideoItemService

            return VideoItemService.delete_video
        if operation is BatchOperationType.CLIP_COLLECTION_ADD:
            from src.service.collections.clip_collection_service import (
                ClipCollectionService,
            )

            collection_id = request.target_collection_id
            return lambda clip_id: ClipCollectionService.add_clip(collection_id, clip_id)
        if operation is BatchOperationType.CLIP_COLLECTION_REMOVE:
            from src.service.collections.clip_collection_service import (
                ClipCollectionService,
            )

            collection_id = request.target_collection_id
            return lambda clip_id: ClipCollectionService.remove_clip(
                collection_id, clip_id
            )
        if operation is BatchOperationType.MOMENT_COLLECTION_ADD:
            from src.service.collections.moment_collection_service import (
                MomentCollectionService,
            )

            collection_id = request.target_collection_id
            return lambda point_id: MomentCollectionService.add_point(
                collection_id, point_id
            )
        if operation is BatchOperationType.MOMENT_COLLECTION_REMOVE:
            from src.service.collections.moment_collection_service import (
                MomentCollectionService,
            )

            collection_id = request.target_collection_id
            return lambda point_id: MomentCollectionService.remove_point(
                collection_id, point_id
            )
        if operation is BatchOperationType.VIDEO_COLLECTION_ADD:
            from src.service.videos.video_collection_service import (
                VideoCollectionService,
            )

            collection_id = request.target_collection_id
            return lambda video_id: VideoCollectionService.add_video(
                collection_id, video_id
            )
        if operation is BatchOperationType.VIDEO_COLLECTION_REMOVE:
            from src.service.videos.video_collection_service import (
                VideoCollectionService,
            )

            collection_id = request.target_collection_id
            return lambda video_id: VideoCollectionService.remove_videos(
                collection_id, [video_id]
            )
        if operation is BatchOperationType.DOWNLOAD_TASK_DELETE:
            from src.service.transfers.downloads.task_service import (
                DownloadTaskService,
            )

            delete_files = request.delete_files
            return lambda task_id: DownloadTaskService.delete_task(
                task_id, delete_files=delete_files
            )
        raise ApiError(422, "unsupported_batch_operation", "不支持的批量操作类型")

    @staticmethod
    def _failure_message(exc: Exception) -> str:
        if isinstance(exc, ApiError):
            return exc.message
        message = str(exc).strip()
        return message or exc.__class__.__name__
