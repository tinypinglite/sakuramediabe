import json

import pytest

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.model import (
    BackgroundTaskRun,
    MediaLibrary,
    SystemEvent,
    SystemNotification,
    VideoCollection,
    VideoImportJob,
)
from src.service.transfers.import_runner import DownloadImportRunner
from src.service.videos.video_import_job_service import VideoImportJobService


@pytest.fixture(autouse=True)
def allow_tmp_browse_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(settings.media_import, "browse_roots", [str(tmp_path)])


@pytest.fixture()
def video_import_job_tables(test_db):
    models = [
        MediaLibrary,
        BackgroundTaskRun,
        VideoCollection,
        VideoImportJob,
        SystemEvent,
        SystemNotification,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


@pytest.fixture(autouse=True)
def stub_runner_submit(monkeypatch):
    calls = []

    def _fake_submit(video_import_job_id, fn, *args, **kwargs):
        calls.append((video_import_job_id, args, kwargs))
        return None

    monkeypatch.setattr(DownloadImportRunner, "submit", staticmethod(_fake_submit))
    return calls


def _create_library(tmp_path) -> MediaLibrary:
    return MediaLibrary.create(name="Videos", root_path=str(tmp_path / "library"))


def test_trigger_creates_job_and_task_run(video_import_job_tables, tmp_path, stub_runner_submit):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()

    result = VideoImportJobService.trigger_directory_import(
        library.id, str(source_dir), transfer_mode="cleanup-source"
    )

    assert result.status == "accepted"
    job = VideoImportJob.get_by_id(result.video_import_job_id)
    assert job.state == "pending"
    assert job.transfer_mode == "cleanup-source"
    assert job.source_path == str(source_dir.resolve())
    assert job.task_run_id == result.task_run_id
    task_run = BackgroundTaskRun.get_by_id(result.task_run_id)
    assert task_run.task_key == "video_directory_import"
    assert task_run.mutex_key is not None
    assert len(stub_runner_submit) == 1


def test_trigger_conflict_when_same_source_running(video_import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()

    VideoImportJobService.trigger_directory_import(library.id, str(source_dir))
    with pytest.raises(ApiError) as exc_info:
        VideoImportJobService.trigger_directory_import(library.id, str(source_dir))

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "video_import_conflict"


def test_trigger_rejects_source_outside_allowed_roots(video_import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    with pytest.raises(ApiError) as exc_info:
        VideoImportJobService.trigger_directory_import(library.id, "/etc")
    assert exc_info.value.status_code == 403
    assert VideoImportJob.select().count() == 0


def test_trigger_rejects_invalid_transfer_mode(video_import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    with pytest.raises(ApiError) as exc_info:
        VideoImportJobService.trigger_directory_import(library.id, str(source_dir), transfer_mode="move")
    assert exc_info.value.code == "invalid_transfer_mode"


def test_trigger_rejects_cleanup_source_inside_library(video_import_job_tables, tmp_path):
    # cleanup-source 指向媒体库目录内时，触发阶段即拒绝（不必等后台作业失败），且不落作业。
    library = _create_library(tmp_path)
    inside = tmp_path / "library" / "incoming"
    inside.mkdir(parents=True)
    with pytest.raises(ApiError) as exc_info:
        VideoImportJobService.trigger_directory_import(
            library.id, str(inside), transfer_mode="cleanup-source"
        )
    assert exc_info.value.code == "cleanup_source_inside_media_library"
    assert VideoImportJob.select().count() == 0


def test_trigger_rejects_missing_collection(video_import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    with pytest.raises(ApiError) as exc_info:
        VideoImportJobService.trigger_directory_import(library.id, str(source_dir), collection_id=999)
    assert exc_info.value.code == "video_collection_not_found"


def test_get_job_returns_failed_files(video_import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    job = VideoImportJob.create(
        source_path=str(tmp_path / "incoming"),
        library=library,
        state="failed",
        failed_count=1,
        failed_files=json.dumps(
            [{"path": "/x/a.mp4", "reason": "media_import_failed", "detail": "boom", "kind": "file"}],
            ensure_ascii=False,
        ),
    )
    resource = VideoImportJobService.get_job(job.id)
    assert resource.state == "failed"
    assert len(resource.failed_files) == 1
    assert resource.failed_files[0].path == "/x/a.mp4"
    assert resource.failed_files[0].reason == "media_import_failed"


def test_recover_orphaned_jobs_marks_failed(video_import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    job = VideoImportJob.create(
        source_path=str(tmp_path / "incoming"),
        library=library,
        state="running",
    )
    result = VideoImportJobService.recover_orphaned_jobs()
    assert result["recovered_count"] == 1
    job_after = VideoImportJob.get_by_id(job.id)
    assert job_after.state == "failed"
    assert job_after.failed_count >= 1
