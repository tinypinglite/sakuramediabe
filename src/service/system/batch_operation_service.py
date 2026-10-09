"""用户批量操作的队列任务执行器。

每笔提交直接创建一行 pending 任务（不占 mutex），由专属 ``batch`` 单并发道按提交
顺序串行领取执行；后来的批量操作自然排队，不存在并发写争抢。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.api.exception.errors import ApiError
from src.schema.system.batch_operations import (
    BatchOperationAcceptedResponse,
    BatchOperationRequest,
    BatchOperationType,
)
from src.service.system import ActivityService


class BatchOperationTaskService:
    TASK_KEY = "batch_operation"

    OPERATION_LABELS: dict[BatchOperationType, str] = {
        BatchOperationType.MEDIA_DELETE: "批量删除媒体",
        BatchOperationType.CLIP_DELETE: "批量删除切片",
        BatchOperationType.MEDIA_POINT_DELETE: "批量删除时刻",
        BatchOperationType.VIDEO_DELETE: "批量删除视频",
        BatchOperationType.CLIP_COLLECTION_ADD: "批量加入切片合集",
        BatchOperationType.CLIP_COLLECTION_REMOVE: "批量移出切片合集",
        BatchOperationType.MOMENT_COLLECTION_ADD: "批量加入时刻合集",
        BatchOperationType.MOMENT_COLLECTION_REMOVE: "批量移出时刻合集",
        BatchOperationType.VIDEO_COLLECTION_ADD: "批量加入视频合集",
        BatchOperationType.VIDEO_COLLECTION_REMOVE: "批量移出视频合集",
        BatchOperationType.DOWNLOAD_TASK_DELETE: "批量删除下载任务",
    }

    @classmethod
    def enqueue(cls, request: BatchOperationRequest) -> BatchOperationAcceptedResponse:
        task = ActivityService.create_task_run(
            task_key=cls.TASK_KEY,
            task_name=cls.OPERATION_LABELS[request.operation],
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
        label = cls.OPERATION_LABELS[request.operation]
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
                text=f"{label} {index}/{total}",
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

            return MediaService.delete_media
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
            return lambda video_id: VideoCollectionService.add_item(
                collection_id, video_id
            )
        if operation is BatchOperationType.VIDEO_COLLECTION_REMOVE:
            from src.service.videos.video_collection_service import (
                VideoCollectionService,
            )

            collection_id = request.target_collection_id
            return lambda item_id: VideoCollectionService.remove_item(
                collection_id, item_id
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
