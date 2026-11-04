from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_SKIPPED,
)
from src.model import (
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    MediaLibrary,
)
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
    ProviderUnavailableError,
)
from src.schema.transfers.media_import import ImportRequest, ImportResult
from src.service.system.task_queue_service import TaskQueueService
from src.service.transfers.shared.import_task_service import ImportTaskService


def test_partial_failure_marks_download_failed_and_notifies_once(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="library", provider_key="test", provider_config={}
    )
    client = DownloadClient.create(name="client", library=library, provider_config={})
    task = DownloadTask.create(
        client=client,
        name="TEST-001",
        movie="TEST-001",
        remote_id="partial-failure",
        state="completed",
        completed_source_ref={"source": "TEST-001"},
        import_status="running",
    )
    notices = []
    monkeypatch.setattr(
        "src.service.transfers.imports.import_service.MediaImportService.import_from_source",
        lambda *_a, **_k: ImportResult(
            imported_count=1,
            failed_count=1,
            new_playable_movies=[{"movie_id": 1, "movie_number": "TEST-001"}],
        ),
    )
    monkeypatch.setattr(
        "src.service.transfers.shared.import_task_service.create_new_media_reminder",
        lambda **kwargs: notices.append(kwargs),
    )
    reporter = SimpleNamespace(progress_callback=None, task_run_id=42)

    summary = ImportTaskService.execute(
        reporter,
        {
            "media_kind": "jav",
            "library_id": library.id,
            "source_ref": {"source": "TEST-001"},
            "download_task_id": task.id,
        },
    )

    assert summary["failed_count"] == 1
    assert DownloadTask.get_by_id(task.id).import_status == "failed"
    assert notices[0]["related_task_run_id"] == 42


def test_only_skipped_files_marks_download_skipped(monkeypatch):
    statuses = []
    monkeypatch.setattr(
        ImportTaskService,
        "_set_download_status",
        lambda task_id, status: statuses.append((task_id, status)),
    )
    monkeypatch.setattr(
        "src.service.transfers.imports.import_service.MediaImportService.import_from_source",
        lambda *_a, **_k: ImportResult(
            imported_count=0, skipped_count=2, failed_count=0
        ),
    )
    reporter = SimpleNamespace(progress_callback=None, task_run_id=43)

    summary = ImportTaskService.execute(
        reporter,
        {
            "media_kind": "jav",
            "library_id": 1,
            "source_ref": {"source": "only-skipped"},
            "download_task_id": 7,
        },
    )

    assert summary["skipped_count"] == 2
    assert statuses == [(7, IMPORT_STATUS_SKIPPED)]


def test_successful_import_keeps_download_task(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="library", provider_key="test", provider_config={}
    )
    client = DownloadClient.create(name="client", library=library, provider_config={})
    task = DownloadTask.create(
        client=client,
        name="TEST-002",
        movie="TEST-002",
        remote_id="remote-task-2",
        state="completed",
        completed_source_ref={"source": "TEST-002"},
        import_status="running",
    )
    monkeypatch.setattr(
        "src.service.transfers.imports.import_service.MediaImportService.import_from_source",
        lambda *_a, **_k: ImportResult(imported_count=1, failed_count=0),
    )

    ImportTaskService.execute(
        SimpleNamespace(progress_callback=None, task_run_id=44),
        {
            "media_kind": "jav",
            "library_id": library.id,
            "source_ref": {"source": "TEST-002"},
            "download_task_id": task.id,
        },
    )

    # 导入成功只写导入状态；远端任务记录与文件由用户自行管理，宿主不再自动删除。
    assert DownloadTask.get_by_id(task.id).import_status == IMPORT_STATUS_COMPLETED


def test_batch_progress_counts_download_tasks_and_continues_after_failure(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="library", provider_key="test", provider_config={}
    )
    client = DownloadClient.create(name="client", library=library, provider_config={})
    failed_task = DownloadTask.create(
        client=client,
        name="TEST-003",
        movie="TEST-003",
        remote_id="remote-task-3",
        state="completed",
        completed_source_ref={"source": "TEST-003"},
        import_status="pending",
    )
    successful_task = DownloadTask.create(
        client=client,
        name="TEST-004",
        movie="TEST-004",
        remote_id="remote-task-4",
        state="completed",
        completed_source_ref={"source": "TEST-004"},
        import_status="pending",
    )
    progress = []
    reporter = SimpleNamespace(
        task_run_id=45,
        progress_callback=None,
        emit=lambda **payload: progress.append(payload),
    )

    def import_source(_self, source_ref, *_args, **_kwargs):
        if source_ref["source"] == "TEST-003":
            raise RuntimeError("one_task_failed")
        return ImportResult(imported_count=1)

    monkeypatch.setattr(
        "src.service.transfers.imports.import_service.MediaImportService.import_from_source",
        import_source,
    )

    summary = ImportTaskService.execute(
        reporter,
        {
            "download_tasks": [
                {
                    "download_task_id": failed_task.id,
                    "media_kind": "jav",
                    "library_id": library.id,
                    "source_ref": {"source": "TEST-003"},
                    "source_disposition": "keep",
                },
                {
                    "download_task_id": successful_task.id,
                    "media_kind": "jav",
                    "library_id": library.id,
                    "source_ref": {"source": "TEST-004"},
                    "source_disposition": "keep",
                },
            ]
        },
    )

    assert [item["current"] for item in progress] == [0, 1, 2]
    assert progress[-1]["text"] == "已处理下载任务 2/2"
    assert summary["download_task_count"] == 2
    assert summary["processed_download_task_count"] == 2
    assert summary["failed_download_task_count"] == 1
    assert DownloadTask.get_by_id(failed_task.id).import_status == "failed"
    assert DownloadTask.get_by_id(successful_task.id).import_status == IMPORT_STATUS_COMPLETED


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("has_new_movies", [False, True])
def test_import_notification_reports_unique_movie_count(monkeypatch, batch, has_new_movies):
    notices = []
    statuses = []

    def import_source(_self, source_ref, *_args, **_kwargs):
        task_id = source_ref["id"]
        if task_id == 2:
            raise RuntimeError("one_task_failed")
        return ImportResult(
            imported_count=2 if has_new_movies else 0,
            new_playable_movies=(
                [
                    {"movie_number": "TEST-001"},
                    {"movie_number": f"TEST-{task_id + 1:03d}"},
                ]
                if has_new_movies else []
            ),
        )

    monkeypatch.setattr(
        "src.service.transfers.imports.import_service.MediaImportService.import_from_source",
        import_source,
    )
    monkeypatch.setattr(
        ImportTaskService, "_set_download_status",
        lambda task_id, status: statuses.append((task_id, status)),
    )
    monkeypatch.setattr(
        "src.service.transfers.shared.import_notifications.NotificationService.create_once",
        lambda draft: notices.append(draft),
    )
    items = [
        {
            "download_task_id": task_id,
            "media_kind": "jav",
            "library_id": 1,
            "source_ref": {"id": task_id},
        }
        for task_id in (1, 2, 3)
    ]
    reporter = SimpleNamespace(
        task_run_id=46, progress_callback=None, emit=lambda **_kwargs: None,
    )

    summary = ImportTaskService.execute(
        reporter, {"download_tasks": items, "library_id": 1} if batch else items[0],
    )

    if batch:
        assert summary["processed_download_task_count"] == 3
        assert summary["failed_download_task_count"] == 1
        assert (2, "failed") in statuses
    if has_new_movies:
        assert len(notices) == 1
        assert notices[0].content == f"新增了 {3 if batch else 2} 个影片"
        assert notices[0].related_task_run_id == 46
    else:
        assert notices == []


def test_recover_interrupted_downloads_only_resets_running_imports_of_failed_runs(test_db):
    library = MediaLibrary.create(name="recovery", provider_key="test", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    expected = {}
    for index, (task_key, run_state, import_status) in enumerate([
        ("library_import", "failed", "running"),
        ("library_import", "failed", "completed"),
        ("library_import", "failed", "failed"),
        ("library_import", "failed", "skipped"),
        ("library_import", "running", "running"),
        ("library_import", "pending", "running"),
        ("library_import", "completed", "running"),
        ("other_task", "failed", "running"),
    ]):
        run = BackgroundTaskRun.create(
            task_key=task_key, task_name="import", trigger_type="internal", state=run_state,
            params={"_staged_receipts": {"old-operation": {"receipt": {}, "committed": False}}},
        )
        task = DownloadTask.create(
            client=client, name=str(index), remote_id=str(index), movie="TEST-001",
            state="completed", import_status=import_status, import_task_run=run,
        )
        expected[task.id] = "pending" if index == 0 else import_status

    assert ImportTaskService.recover_interrupted_downloads() == 1
    assert {task.id: task.import_status for task in DownloadTask.select()} == expected
    assert ImportTaskService.recover_interrupted_downloads() == 0


def test_enqueue_queues_multiple_imports_for_same_library(test_db):
    library = MediaLibrary.create(
        name="queue", provider_key="test", provider_config={}
    )
    first = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="video",
            library_id=library.id,
            source_ref={"kind": "manual_source", "relative_path": "a.mp4"},
        )
    )
    second = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="video",
            library_id=library.id,
            source_ref={"kind": "manual_source", "relative_path": "b.mp4"},
        )
    )

    assert first.state == "pending"
    assert second.state == "pending"
    runs = list(
        BackgroundTaskRun.select()
        .where(BackgroundTaskRun.scope_key == f"library_import:{library.id}")
        .order_by(BackgroundTaskRun.id.asc())
    )
    assert [run.id for run in runs] == [first.task_run_id, second.task_run_id]

    claimed = TaskQueueService.claim_next(
        include_task_keys={ImportTaskService.TASK_KEY}
    )
    assert claimed is not None and claimed.id == first.task_run_id
    # 同库第二个任务保持排队，等第一个完成后才会被领取。
    assert (
        TaskQueueService.claim_next(include_task_keys={ImportTaskService.TASK_KEY})
        is None
    )


def test_enqueue_rejects_download_task_already_importing(test_db):
    library = MediaLibrary.create(
        name="busy", provider_key="test", provider_config={}
    )
    client = DownloadClient.create(name="client", library=library, provider_config={})
    task = DownloadTask.create(
        client=client,
        name="TEST-001",
        movie="TEST-001",
        remote_id="busy-import",
        state="completed",
        completed_source_ref={"version": 1, "kind": "cloud115_dir", "cid": "1"},
        import_status=IMPORT_STATUS_PENDING,
    )
    request = ImportRequest(
        media_kind="jav",
        library_id=library.id,
        source_ref=task.completed_source_ref,
    )
    ImportTaskService.enqueue(
        request,
        download_task_id=task.id,
        allowed_download_statuses={IMPORT_STATUS_PENDING},
    )

    # 未经接口层预检直接重复入队：事务内锁行读到的状态已变成 running，必须拒绝。
    with pytest.raises(ApiError) as caught:
        ImportTaskService.enqueue(
            request,
            download_task_id=task.id,
            allowed_download_statuses={IMPORT_STATUS_PENDING},
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "download_task_import_conflict"
    assert DownloadTask.get_by_id(task.id).import_status == "running"
    assert (
        BackgroundTaskRun.select()
        .where(BackgroundTaskRun.task_key == ImportTaskService.TASK_KEY)
        .count()
        == 1
    )


class _OverlapStorage:
    def validate_import_source(self, *, source_ref, source_disposition):
        raise ProviderOperationError(
            provider_key="local",
            operation="validate_import_source",
            code="invalid_config",
            safe_message="导入源与媒体库目录重叠",
            retryable=False,
        )


def _enqueue_library(name: str) -> MediaLibrary:
    return MediaLibrary.create(name=name, provider_key="test", provider_config={})


def test_enqueue_rejects_overlapping_import_source_before_creating_task(
    test_db, monkeypatch
):
    library = _enqueue_library("overlap")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: _OverlapStorage()
    )

    with pytest.raises(ApiError) as caught:
        ImportTaskService.enqueue(
            ImportRequest(
                media_kind="video",
                library_id=library.id,
                source_ref={"kind": "manual_source", "relative_path": "media"},
            )
        )

    assert caught.value.status_code == 422
    assert caught.value.code == "provider_invalid_config"
    assert BackgroundTaskRun.select().count() == 0


def test_enqueue_validation_failure_does_not_touch_download_task(
    test_db, monkeypatch
):
    library = _enqueue_library("overlap-download")
    client = DownloadClient.create(
        name="client", library=library, provider_config={}
    )
    task = DownloadTask.create(
        client=client,
        name="TEST-001",
        movie="TEST-001",
        remote_id="overlap-download",
        state="completed",
        completed_source_ref={"kind": "manual_source", "relative_path": "media"},
        import_status=IMPORT_STATUS_PENDING,
    )
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: _OverlapStorage()
    )

    with pytest.raises(ApiError) as caught:
        ImportTaskService.enqueue(
            ImportRequest(
                media_kind="jav",
                library_id=library.id,
                source_ref=task.completed_source_ref,
            ),
            download_task_id=task.id,
            allowed_download_statuses={IMPORT_STATUS_PENDING},
        )

    assert caught.value.code == "provider_invalid_config"
    assert DownloadTask.get_by_id(task.id).import_status == IMPORT_STATUS_PENDING
    assert BackgroundTaskRun.select().count() == 0


def test_enqueue_skips_validation_for_provider_without_capability(
    test_db, monkeypatch
):
    library = _enqueue_library("no-validation")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: SimpleNamespace()
    )

    response = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="video",
            library_id=library.id,
            source_ref={"kind": "manual_source", "relative_path": "video.mp4"},
        )
    )

    assert response.state == "pending"
    assert BackgroundTaskRun.select().count() == 1


def test_enqueue_keeps_legacy_behavior_when_provider_not_installed(
    test_db, monkeypatch
):
    library = _enqueue_library("missing-provider")

    def unavailable(_handle):
        raise ProviderUnavailableError("test")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", unavailable)

    response = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="video",
            library_id=library.id,
            source_ref={"kind": "manual_source", "relative_path": "video.mp4"},
        )
    )

    assert response.state == "pending"
    assert BackgroundTaskRun.select().count() == 1


def test_enqueue_maps_provider_build_failure_to_api_error(test_db, monkeypatch):
    library = _enqueue_library("bad-provider")

    def invalid_config(_handle):
        raise ProviderOperationError(
            provider_key="test",
            operation="build_storage",
            code="invalid_config",
            safe_message="配置无效",
            retryable=False,
        )

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", invalid_config)

    with pytest.raises(ApiError) as caught:
        ImportTaskService.enqueue(
            ImportRequest(
                media_kind="video",
                library_id=library.id,
                source_ref={"kind": "manual_source", "relative_path": "video.mp4"},
            )
        )

    assert caught.value.status_code == 422
    assert caught.value.code == "provider_invalid_config"
    assert BackgroundTaskRun.select().count() == 0
