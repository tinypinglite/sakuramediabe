"""批量操作任务执行器的队列与执行语义护栏。"""

from src.api.exception.errors import ApiError
from src.model import BackgroundTaskRun
from src.scheduler.queue_tasks import (
    LANE_BATCH,
    LANE_CONCURRENCY,
    NON_DEFAULT_LANE_TASK_KEYS,
    lane_task_keys,
)
from src.schema.system.batch_operations import BatchOperationRequest
from src.service.system.batch_operation_service import BatchOperationTaskService
from src.service.system.task_queue_service import TaskQueueService


class _RecordingReporter:
    def __init__(self):
        self.events: list[tuple[int | None, int | None, str | None]] = []

    def emit(self, *, current=None, total=None, text=None, summary_patch=None):
        self.events.append((current, total, text))


def _params(operation: str, ids: list[int], **extra):
    payload = {"operation": operation, "ids": ids}
    payload.update(extra)
    return BatchOperationRequest.model_validate(payload).model_dump(mode="json")


def test_batch_operation_uses_dedicated_single_worker_lane():
    assert LANE_CONCURRENCY[LANE_BATCH] == 1
    assert lane_task_keys(LANE_BATCH) == {BatchOperationTaskService.TASK_KEY}
    assert BatchOperationTaskService.TASK_KEY in NON_DEFAULT_LANE_TASK_KEYS


def test_enqueue_creates_pending_rows_in_fifo_order(test_db):
    first = BatchOperationTaskService.enqueue(
        BatchOperationRequest(operation="media_delete", ids=[1])
    )
    second = BatchOperationTaskService.enqueue(
        BatchOperationRequest(operation="media_delete", ids=[2])
    )

    assert first.state == "pending"
    assert second.state == "pending"
    assert first.task_run_id != second.task_run_id
    # 单并发道按 id 顺序领取：先提交的先跑，后提交的保持 pending 排队。
    claimed = TaskQueueService.claim_next(include_task_keys=lane_task_keys(LANE_BATCH))
    assert claimed is not None and claimed.id == first.task_run_id
    assert BackgroundTaskRun.get_by_id(second.task_run_id).state == "pending"


def test_execute_records_partial_failures_and_continues(monkeypatch):
    from src.service.playback.media_service import MediaService

    calls: list[int] = []

    def fake_delete_media(media_id: int, *, sync_video_member: bool = True) -> None:
        calls.append(media_id)
        if media_id == 2:
            raise ApiError(404, "media_not_found", "媒体不存在")

    monkeypatch.setattr(MediaService, "delete_media", fake_delete_media)
    reporter = _RecordingReporter()

    result = BatchOperationTaskService.execute(
        reporter, _params("media_delete", [1, 2, 3])
    )

    assert calls == [1, 2, 3]
    assert result == {
        "operation": "media_delete",
        "total": 3,
        "succeeded_count": 2,
        "failed_count": 1,
        "failed": [{"id": 2, "message": "媒体不存在"}],
    }
    assert [event[0] for event in reporter.events] == [1, 2, 3]
    assert all(event[1] == 3 for event in reporter.events)


def test_execute_dispatches_collection_membership_with_target(monkeypatch):
    from src.service.collections.clip_collection_service import ClipCollectionService

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        ClipCollectionService,
        "add_clip",
        lambda collection_id, clip_id: calls.append((collection_id, clip_id)),
    )

    result = BatchOperationTaskService.execute(
        _RecordingReporter(),
        _params("clip_collection_add", [5, 6], target_collection_id=9),
    )

    assert calls == [(9, 5), (9, 6)]
    assert result["succeeded_count"] == 2
    assert result["failed_count"] == 0


def test_execute_dispatches_download_task_delete_with_files_flag(monkeypatch):
    from src.service.transfers.downloads.task_service import DownloadTaskService

    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        DownloadTaskService,
        "delete_task",
        lambda task_id, *, delete_files: calls.append((task_id, delete_files)),
    )

    result = BatchOperationTaskService.execute(
        _RecordingReporter(),
        _params("download_task_delete", [7, 8], delete_files=True),
    )

    assert calls == [(7, True), (8, True)]
    assert result["succeeded_count"] == 2
