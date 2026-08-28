from types import SimpleNamespace

from src.common.media_import_status import (
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_SKIPPED,
)
from src.model import (
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
