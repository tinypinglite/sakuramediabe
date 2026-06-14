import json
import os

import pytest

from src.api.exception.errors import ApiError
from src.config.config import settings
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


@pytest.fixture(autouse=True)
def allow_tmp_browse_roots(monkeypatch, tmp_path):
    # 测试目录不在默认 /mnt 白名单内，放开为当前用例的 tmp_path。
    monkeypatch.setattr(settings.media_import, "browse_roots", [str(tmp_path)])


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


def test_trigger_directory_import_rejects_source_outside_allowed_roots(import_job_tables, tmp_path):
    library = _create_library(tmp_path)

    # /etc 不在白名单根（tmp_path）内，应被拒绝且不建作业。
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
    # 失败条目带分类，metadata 失败属可操作的单文件失败。
    assert detail.failed_files[0].kind == "file"


def test_delete_failed_file_rejects_task_level_directory_entry(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    # 任务级失败把源目录写入 failed_files（kind=job），不应被当作文件删除。
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(source_dir), "reason": "import_job_crashed", "detail": "boom"}],
    )

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.delete_failed_file(job.id, str(source_dir))

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "failed_file_not_actionable"
    # 目录仍在，未被误删。
    assert source_dir.is_dir()


def test_delete_failed_file_rejects_directory_even_if_marked_file(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    a_dir = source_dir / "ABP-123"
    a_dir.mkdir()
    # kind=file 但路径实际是目录时，is_dir 守卫兜底拒绝，杜绝 IsADirectoryError 500。
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(a_dir), "reason": "media_import_failed", "detail": "", "kind": "file"}],
    )

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.delete_failed_file(job.id, str(a_dir))

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "cannot_delete_directory"
    assert a_dir.is_dir()


def test_rename_failed_file_rejects_task_level_directory_entry(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(source_dir), "reason": "import_job_crashed", "detail": "boom"}],
    )

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.rename_failed_file(job.id, str(source_dir), "renamed")

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "failed_file_not_actionable"
    assert source_dir.is_dir()


def test_failed_file_operations_reject_skipped_kind(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    small_file = source_dir / "ABP-123.mp4"
    small_file.write_bytes(b"x")
    # file_too_small 属 skipped 分类，不可被删除/重命名/重导。
    job = _create_failed_job(
        library,
        source_dir,
        [{"path": str(small_file), "reason": "file_too_small", "detail": ""}],
    )

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.delete_failed_file(job.id, str(small_file))
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "failed_file_not_actionable"

    with pytest.raises(ApiError) as retry_exc:
        MediaImportJobService.retry_failed_files(job.id, [str(small_file)])
    assert retry_exc.value.status_code == 403


def test_failed_file_operations_reject_non_terminal_job(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    failed_file = source_dir / "ABP-123.mp4"
    failed_file.write_bytes(b"data")
    # 运行中的作业不允许操作失败文件。
    job = ImportJob.create(
        source_path=str(source_dir),
        library=library,
        state="running",
        failed_count=1,
        failed_files=json.dumps(
            [{"path": str(failed_file), "reason": "metadata_fetch_failed", "detail": "", "kind": "file"}],
            ensure_ascii=False,
        ),
    )

    for action in (
        lambda: MediaImportJobService.delete_failed_file(job.id, str(failed_file)),
        lambda: MediaImportJobService.rename_failed_file(job.id, str(failed_file), "ABP-9.mp4"),
        lambda: MediaImportJobService.retry_failed_files(job.id, [str(failed_file)]),
    ):
        with pytest.raises(ApiError) as exc_info:
            action()
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "job_in_progress"


@pytest.mark.parametrize("bad_name", [".", "..", ".hidden.mp4", "bad\x00name.mp4"])
def test_rename_failed_file_rejects_invalid_new_name(import_job_tables, tmp_path, bad_name):
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
        MediaImportJobService.rename_failed_file(job.id, str(failed_file), bad_name)

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_new_name"
    assert failed_file.exists()


def test_retry_uses_original_transfer_mode(import_job_tables, tmp_path, stub_runner_submit):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    failed_path = str(source_dir / "ABP-123.mp4")
    job = ImportJob.create(
        source_path=str(source_dir),
        library=library,
        state="failed",
        transfer_mode="cleanup-source",
        failed_count=1,
        failed_files=json.dumps(
            [{"path": failed_path, "reason": "metadata_fetch_failed", "detail": "", "kind": "file"}],
            ensure_ascii=False,
        ),
    )

    MediaImportJobService.retry_failed_files(job.id, [failed_path])

    # 提交参数：(library_id, source, import_job_id, task_run_id, transfer_mode, only_files)
    _, args, _ = stub_runner_submit[-1]
    assert args[-2] == "cleanup-source"
    assert args[-1] == [failed_path]


def test_launch_import_compensates_when_submit_fails(import_job_tables, tmp_path, monkeypatch):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()

    def _raise_submit(*args, **kwargs):
        raise RuntimeError("queue boom")

    monkeypatch.setattr(DownloadImportRunner, "submit", staticmethod(_raise_submit))

    with pytest.raises(ApiError) as exc_info:
        MediaImportJobService.trigger_directory_import(library.id, str(source_dir))

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "media_import_failed"
    # 入队失败后作业标记为 failed，task_run 被回收且释放 mutex_key（避免后续永久 409）。
    job = ImportJob.get(ImportJob.source_path == str(source_dir.resolve()))
    assert job.state == "failed"
    # 终态作业补一条任务级失败明细，详情页不会出现 failed 却无原因。
    assert job.failed_count >= 1
    assert any(
        item["reason"] == "import_job_bootstrap_failed" for item in json.loads(job.failed_files)
    )
    task_run = BackgroundTaskRun.get_by_id(job.task_run_id)
    assert task_run.state == "failed"
    assert task_run.mutex_key is None


def test_recover_orphaned_jobs_resets_dead_manual_job(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    # owner_pid 指向一个不存在的进程，模拟 API 进程已崩溃。
    task_run = BackgroundTaskRun.create(
        task_key="media_directory_import",
        task_name="目录导入",
        trigger_type="manual",
        state="running",
        owner_pid=2**31 - 1,
    )
    orphan = ImportJob.create(
        source_path=str(source_dir),
        library=library,
        state="running",
        task_run=task_run,
    )

    result = MediaImportJobService.recover_orphaned_jobs()

    assert result["recovered_count"] == 1
    refreshed = ImportJob.get_by_id(orphan.id)
    assert refreshed.state == "failed"
    assert refreshed.finished_at is not None
    assert any(item["reason"] == "import_job_interrupted" for item in json.loads(refreshed.failed_files))
    assert BackgroundTaskRun.get_by_id(task_run.id).state == "failed"


def test_recover_orphaned_jobs_skips_live_owner_job(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    # owner_pid 指向当前存活进程，模拟 API 进程仍在运行该导入，恢复时应跳过不误杀。
    task_run = BackgroundTaskRun.create(
        task_key="media_directory_import",
        task_name="目录导入",
        trigger_type="manual",
        state="running",
        owner_pid=os.getpid(),
    )
    job = ImportJob.create(
        source_path=str(source_dir),
        library=library,
        state="running",
        task_run=task_run,
    )

    result = MediaImportJobService.recover_orphaned_jobs()

    assert result["recovered_count"] == 0
    assert ImportJob.get_by_id(job.id).state == "running"


def test_recover_orphaned_jobs_skips_download_task_jobs(import_job_tables, tmp_path):
    library = _create_library(tmp_path)
    client = DownloadClient.create(
        name="c1",
        base_url="http://x",
        username="u",
        password="p",
        client_save_path="/dl",
        local_root_path=str(tmp_path / "dl"),
        media_library=library,
    )
    download_task = DownloadTask.create(
        client=client,
        name="t",
        info_hash="hash1",
        save_path="/dl/t",
        import_status="running",
    )
    # 关联下载任务的导入由下载侧 handler 负责，这里不应处理。
    job = ImportJob.create(
        source_path=str(tmp_path / "incoming"),
        library=library,
        download_task=download_task,
        state="running",
    )

    result = MediaImportJobService.recover_orphaned_jobs()

    assert result["recovered_count"] == 0
    assert ImportJob.get_by_id(job.id).state == "running"


def test_mark_import_failed_skips_terminal_job(import_job_tables, tmp_path):
    # 统一带终态守卫：作业已是终态（failed/completed）时，_mark_import_failed 不再追加重复失败明细。
    library = _create_library(tmp_path)
    existing = [{"path": "/x/a.mp4", "reason": "media_import_failed", "detail": "boom", "kind": "file"}]
    for terminal_state in ("failed", "completed"):
        job = ImportJob.create(
            source_path=str(tmp_path / "incoming"),
            library=library,
            state=terminal_state,
            failed_count=1,
            failed_files=json.dumps(existing, ensure_ascii=False),
        )
        MediaImportJobService._mark_import_failed(job.id, "late boom")
        after = ImportJob.get_by_id(job.id)
        assert after.state == terminal_state
        assert json.loads(after.failed_files) == existing


def test_mark_import_failed_marks_non_terminal_job(import_job_tables, tmp_path):
    # 非终态作业（setup 阶段崩溃）仍兜底转 failed 并补一条引导失败明细。
    library = _create_library(tmp_path)
    job = ImportJob.create(
        source_path=str(tmp_path / "incoming"),
        library=library,
        state="running",
    )
    MediaImportJobService._mark_import_failed(job.id, "bootstrap boom")
    after = ImportJob.get_by_id(job.id)
    assert after.state == "failed"
    assert after.failed_count >= 1
    assert len(json.loads(after.failed_files)) == 1
