from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.model import (
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    Media,
    MediaLibrary,
    Movie,
)
from src.schema.transfers.media_import import ImportRequest, ImportResult
from src.service.transfers.shared.import_task_service import ImportTaskService


@pytest.mark.parametrize(
    ("media_kind", "backend", "source"),
    [
        ("jav", "local", {"source_path": "/incoming/JAV"}),
        ("jav", "cloud115", {"source_cid": "jav-cid"}),
        ("video", "local", {"source_path": "/incoming/video.mp4"}),
        ("video", "cloud115", {"source_fid": "video-fid"}),
    ],
)
def test_execute_request_dispatches_all_import_combinations(
    monkeypatch, media_kind, backend, source
):
    expected = ImportResult(imported_count=1)

    if (media_kind, backend) == ("jav", "local"):
        from src.service.transfers.imports.import_service import MediaImportService

        monkeypatch.setattr(MediaImportService, "import_from_source", lambda *_a, **_k: expected)
    elif (media_kind, backend) == ("jav", "cloud115"):
        from src.service.transfers.cloud115.importer.service import (
            Cloud115ImportService,
        )

        monkeypatch.setattr(
            Cloud115ImportService, "import_from_cloud115", lambda *_a, **_k: expected
        )
    elif backend == "local":
        from src.service.videos.video_import_service import VideoImportService

        monkeypatch.setattr(VideoImportService, "import_from_source", lambda *_a, **_k: expected)
    else:
        from src.service.videos.cloud115_video_import_service import (
            Cloud115VideoImportService,
        )

        monkeypatch.setattr(
            Cloud115VideoImportService, "import_from_cloud115", lambda *_a, **_k: expected
        )

    request = ImportRequest(
        media_kind=media_kind,
        backend=backend,
        library_id=1,
        **source,
    )
    assert ImportTaskService._execute_request(
        request, progress_callback=None, managed_download_source=False
    ) == expected


def test_local_imports_are_globally_serialized_across_sources_kinds_and_libraries(
    test_db, monkeypatch, tmp_path
):
    first_source = tmp_path / "source-a"
    second_source = tmp_path / "source-b"
    first_source.mkdir()
    second_source.mkdir()
    monkeypatch.setattr(settings.media_import, "browse_roots", [str(tmp_path)])
    first_library = MediaLibrary.create(
        name="local-a", backend="local", backend_config={"root_path": "/library-a"}
    )
    second_library = MediaLibrary.create(
        name="local-b", backend="local", backend_config={"root_path": "/library-b"}
    )

    accepted = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="jav",
            backend="local",
            library_id=first_library.id,
            source_path=str(first_source),
        )
    )
    with pytest.raises(ApiError) as exc_info:
        ImportTaskService.enqueue(
            ImportRequest(
                media_kind="video",
                backend="local",
                library_id=second_library.id,
                source_path=str(second_source),
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details["blocking_task_run_id"] == accepted.task_run_id
    assert BackgroundTaskRun.get_by_id(accepted.task_run_id).mutex_key == "library_import:local"


def test_cloud115_serializes_cid_and_fid_across_media_kinds(test_db):
    library = MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_account_key="cloud115:same-account",
        backend_config={},
    )

    accepted = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="jav",
            backend="cloud115",
            library_id=library.id,
            source_cid="directory-a",
        )
    )
    with pytest.raises(ApiError) as exc_info:
        ImportTaskService.enqueue(
            ImportRequest(
                media_kind="video",
                backend="cloud115",
                library_id=library.id,
                source_fid="file-b",
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details["blocking_task_run_id"] == accepted.task_run_id


def test_different_cloud115_accounts_share_global_mutex(test_db):
    first_library = MediaLibrary.create(
        name="cloud-a",
        backend="cloud115",
        backend_account_key="cloud115:account-a",
        backend_config={},
    )
    second_library = MediaLibrary.create(
        name="cloud-b",
        backend="cloud115",
        backend_account_key="cloud115:account-b",
        backend_config={},
    )

    first = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="jav",
            backend="cloud115",
            library_id=first_library.id,
            source_cid="same-id",
        )
    )
    with pytest.raises(ApiError) as exc_info:
        ImportTaskService.enqueue(
            ImportRequest(
                media_kind="video",
                backend="cloud115",
                library_id=second_library.id,
                source_fid="same-id",
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details["blocking_task_run_id"] == first.task_run_id
    assert BackgroundTaskRun.get_by_id(first.task_run_id).mutex_key == "cloud115_write:global"


def test_local_and_cloud115_imports_can_run_in_parallel(test_db, monkeypatch, tmp_path):
    source = tmp_path / "local-source"
    source.mkdir()
    monkeypatch.setattr(settings.media_import, "browse_roots", [str(tmp_path)])
    local_library = MediaLibrary.create(
        name="local", backend="local", backend_config={"root_path": "/library"}
    )
    cloud_library = MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_account_key="cloud115:parallel-with-local",
        backend_config={},
    )

    local = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="jav",
            backend="local",
            library_id=local_library.id,
            source_path=str(source),
        )
    )
    cloud = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="video",
            backend="cloud115",
            library_id=cloud_library.id,
            source_fid="remote-file",
        )
    )

    assert local.task_run_id != cloud.task_run_id


def test_cloud115_import_and_rapid_upload_share_global_write_mutex(test_db, tmp_path):
    """不同任务类型只要写 115，就必须在入队前互斥。"""
    from src.service.transfers.rapid_upload.command_service import (
        MediaRapidUploadCommandService,
    )

    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"video")
    local_library = MediaLibrary.create(
        name="rapid-source",
        backend="local",
        backend_config={"root_path": str(tmp_path)},
    )
    cloud_library = MediaLibrary.create(
        name="rapid-target",
        backend="cloud115",
        backend_account_key="cloud115:shared-write-account",
        backend_config={"cookies": "test-cookie", "root_cid": "1"},
    )
    movie = Movie.create(movie_number="MUTEX-001", javdb_id="mutex-1", title="mutex")
    media = Media.create(
        movie=movie,
        library=local_library,
        path=str(source_path),
        content_fingerprint="mutex-fingerprint",
    )
    accepted = ImportTaskService.enqueue(
        ImportRequest(
            media_kind="jav",
            backend="cloud115",
            library_id=cloud_library.id,
            source_cid="incoming",
        )
    )

    with pytest.raises(ApiError) as exc_info:
        MediaRapidUploadCommandService.trigger_batch(
            media_ids=[media.id],
            target_library_id=cloud_library.id,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "cloud115_write_task_conflict"
    assert exc_info.value.details == {"blocking_task_run_id": accepted.task_run_id}


def test_partial_failure_marks_download_failed_and_notifies_once(test_db, monkeypatch):
    library = MediaLibrary.create(name="cloud", backend="cloud115", backend_config={})
    client = DownloadClient.create(name="115", kind="cloud115", media_library=library)
    task = DownloadTask.create(
        client=client,
        name="TEST-001",
        info_hash="partial-failure",
        save_path="cloud115:/TEST-001",
        download_state="completed",
        import_status="running",
    )
    notices = []
    monkeypatch.setattr(
        ImportTaskService,
        "_execute_request",
        staticmethod(
            lambda *_a, **_k: ImportResult(
                imported_count=1,
                failed_count=1,
                new_playable_movies=[{"movie_id": 1, "movie_number": "TEST-001"}],
            )
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
            "backend": "cloud115",
            "library_id": library.id,
            "source_cid": "partial-source",
            "download_task_id": task.id,
        },
    )

    assert summary["failed_count"] == 1
    assert DownloadTask.get_by_id(task.id).import_status == "failed"
    assert notices[0]["related_task_run_id"] == 42
