import json

import pytest

from src.api.exception.errors import ApiError
from src.model import (
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    ImportJob,
    MediaLibrary,
    SystemEvent,
    SystemNotification,
)
from src.service.transfers.import_runner import DownloadImportRunner
from src.service.transfers.media_import_job_service import MediaImportJobService


@pytest.fixture()
def import_job_tables(test_db):
    models = [
        MediaLibrary,
        BackgroundTaskRun,
        DownloadClient,
        DownloadTask,
        ImportJob,
        SystemEvent,
        SystemNotification,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


@pytest.fixture(autouse=True)
def stub_runner_submit(monkeypatch):
    # 触发链路只验证 job/task_run 建立与防重，后台执行用空实现替换，避免真实导入。
    calls = []

    def _fake_submit(import_job_id, fn, *args, **kwargs):
        calls.append((import_job_id, args, kwargs))
        return None

    monkeypatch.setattr(DownloadImportRunner, "submit", staticmethod(_fake_submit))
    return calls


def _create_library(tmp_path) -> MediaLibrary:
    return MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))


def _create_failed_job(library: MediaLibrary, source_dir, failed_files) -> ImportJob:
    return ImportJob.create(
        source_path=str(source_dir),
        library=library,
        state="failed",
        failed_count=len(failed_files),
        failed_files=json.dumps(failed_files, ensure_ascii=False),
    )


def test_trigger_directory_import_creates_job_and_task_run(import_job_tables, tmp_path, stub_runner_submit):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()

    result = MediaImportJobService.trigger_directory_import(library.id, str(source_dir))

    assert result.status == "accepted"
    job = ImportJob.get_by_id(result.import_job_id)
    assert job.state == "pending"
    assert job.source_path == str(source_dir.resolve())
    assert job.task_run_id == result.task_run_id
    task_run = BackgroundTaskRun.get_by_id(result.task_run_id)
    assert task_run.task_key == "media_directory_import"
    assert task_run.mutex_key is not None
    assert len(stub_runner_submit) == 1


def test_trigger_directory_import_conflict_when_same_source_running(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()

    MediaImportJobService.trigger_directory_import(library.id, str(source_dir))

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.trigger_directory_import(library.id, str(source_dir))

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_import_conflict"
    assert exc_info.value.details["blocking_task_run_id"] is not None


def test_trigger_directory_import_rejects_blacklisted_source(import_job_tables, tmp_path):
    library = _create_library(tmp_path)

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.trigger_directory_import(library.id, "/etc")

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "path_forbidden"
    assert ImportJob.select().count() == 0


def test_trigger_directory_import_rejects_missing_library(import_job_tables, tmp_path):
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.trigger_directory_import(999, str(source_dir))

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "media_library_not_found"


def test_retry_failed_files_rejects_path_outside_failed_list(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(source_dir / "ABP-123.mp4"), "reason": "metadata_fetch_failed", "detail": ""}],
    )

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.retry_failed_files(job.id, [str(source_dir / "OTHER.mp4")])

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "file_not_in_failed_list"


def test_retry_failed_files_launches_new_job_for_subset(import_job_tables, tmp_path, stub_runner_submit):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    failed_path = str(source_dir / "ABP-123.mp4")
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": failed_path, "reason": "metadata_fetch_failed", "detail": ""}],
    )

    result = MediaImportJobService.retry_failed_files(job.id, [failed_path])

    assert result.import_job_id != job.id
    new_job = ImportJob.get_by_id(result.import_job_id)
    assert new_job.source_path == str(source_dir.resolve())
    # only_files 透传给后台执行的最后一个位置参数。
    _, args, _ = stub_runner_submit[-1]
    assert args[-1] == [failed_path]


def test_delete_failed_file_removes_file_and_list_entry(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    failed_file = source_dir / "ABP-123.mp4"
    failed_file.write_bytes(b"data")
    job = _create_failed_job(
        library,
        source_dir,
        [
            {"path": str(failed_file), "reason": "metadata_fetch_failed", "detail": ""},
            {"path": str(source_dir / "KEEP.mp4"), "reason": "movie_number_not_found", "detail": ""},
        ],
    )

    result = MediaImportJobService.delete_failed_file(job.id, str(failed_file))

    assert not failed_file.exists()
    remaining = {item.path for item in result.failed_files}
    assert str(failed_file) not in remaining
    assert str(source_dir / "KEEP.mp4") in remaining


def test_delete_failed_file_rejects_path_outside_failed_list(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(source_dir / "ABP-123.mp4"), "reason": "metadata_fetch_failed", "detail": ""}],
    )

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.delete_failed_file(job.id, str(source_dir / "EVIL.mp4"))

    assert exc_info.value.status_code == 403


def test_rename_failed_file_renames_and_updates_list(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    failed_file = source_dir / "ABP-123.mp4"
    failed_file.write_bytes(b"data")
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(failed_file), "reason": "movie_number_not_found", "detail": ""}],
    )

    result = MediaImportJobService.rename_failed_file(job.id, str(failed_file), "ABP-999.mp4")

    renamed = source_dir / "ABP-999.mp4"
    assert renamed.exists()
    assert not failed_file.exists()
    assert {item.path for item in result.failed_files} == {str(renamed)}


def test_rename_failed_file_rejects_separator_in_new_name(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    failed_file = source_dir / "ABP-123.mp4"
    failed_file.write_bytes(b"data")
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(failed_file), "reason": "movie_number_not_found", "detail": ""}],
    )

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.rename_failed_file(job.id, str(failed_file), "../escape.mp4")

    assert exc_info.value.status_code == 422
    assert failed_file.exists()


def test_rename_failed_file_conflict_when_target_exists(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    failed_file = source_dir / "ABP-123.mp4"
    failed_file.write_bytes(b"data")
    (source_dir / "ABP-999.mp4").write_bytes(b"existing")
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(failed_file), "reason": "movie_number_not_found", "detail": ""}],
    )

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.rename_failed_file(job.id, str(failed_file), "ABP-999.mp4")

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "rename_target_exists"


def test_list_and_get_job_returns_failed_files(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(source_dir / "ABP-123.mp4"), "reason": "metadata_fetch_failed", "detail": "boom"}],
    )

    page = MediaImportJobService.list_jobs(page=1, page_size=10)
    assert page.total == 1
    assert page.items[0].id == job.id

    detail = MediaImportJobService.get_job(job.id)
    assert detail.failed_files[0].reason == "metadata_fetch_failed"
    assert detail.failed_files[0].detail == "boom"
