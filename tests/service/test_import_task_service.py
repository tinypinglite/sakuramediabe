from types import SimpleNamespace

import pytest

from src.common.media_import_status import (
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_SKIPPED,
)
from src.model import (
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    MediaLibrary,
)
from src.schema.transfers.media_import import ImportResult
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


def test_successful_import_deletes_remote_task_but_keeps_files(test_db, monkeypatch):
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
    deleted = []
    monkeypatch.setattr(
        "src.service.transfers.imports.import_service.MediaImportService.import_from_source",
        lambda *_a, **_k: ImportResult(imported_count=1, failed_count=0),
    )
    monkeypatch.setattr(
        "src.service.transfers.shared.import_task_service.download_provider",
        lambda _client: type(
            "Provider",
            (),
            {
                "delete_task": lambda _self, *, remote_id, delete_files: deleted.append(
                    (remote_id, delete_files)
                )
            },
        )(),
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

    assert DownloadTask.get_by_id(task.id).import_status == IMPORT_STATUS_COMPLETED
    assert deleted == [("remote-task-2", False)]


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
    monkeypatch.setattr(ImportTaskService, "_delete_remote_download_task", lambda _task_id: None)

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
    monkeypatch.setattr(ImportTaskService, "_delete_remote_download_task", lambda _id: None)
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
