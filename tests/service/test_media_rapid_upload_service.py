from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from src.api.exception.errors import ApiError
from src.lib.cloud115.types import FileMeta, RapidUploadResult, RapidUploadStatus
from src.model import (
    BackgroundTaskRun,
    Media,
    MediaLibrary,
    MediaRapidUploadBatch,
    MediaRapidUploadItem,
    SystemNotification,
    VideoItem,
)
from src.service.playback.media_library_service import MediaLibraryService
from src.service.transfers import media_rapid_upload_service as rapid_module
from src.service.playback.media_service import MediaService
from src.service.transfers.media_rapid_upload_service import (
    ITEM_ACTION_CLEANUP_ONLY,
    ITEM_ACTION_RESUME_REMOTE,
    ITEM_STATE_CLEANUP_FAILED,
    ITEM_STATE_FAILED,
    ITEM_STATE_SUCCEEDED,
    MediaRapidUploadService,
)


class _StubRapidUploadClient:
    def __init__(self, *, statuses: list[RapidUploadStatus]):
        self._statuses = iter(statuses)
        self.calls: list[str] = []
        self.names: dict[str, str] = {}
        self.file_info_parent_id = "videos-cid"
        self.delete_error: Exception | None = None

    async def rapid_upload(self, path, *, pid: str):
        source = Path(path)
        status = next(self._statuses)
        self.calls.append(source.name)
        sha1 = "A" * 40
        if status is not RapidUploadStatus.SUCCESS:
            return RapidUploadResult(
                status=status,
                path=str(source),
                filename=source.name,
                size=source.stat().st_size,
                sha1=sha1,
            )
        fid = f"fid-{len(self.calls)}"
        self.names[fid] = source.name
        return RapidUploadResult(
            status=status,
            path=str(source),
            filename=source.name,
            size=source.stat().st_size,
            sha1=sha1,
            file_id=fid,
            pickcode=f"pc-{len(self.calls)}",
        )

    async def file_info(self, fid: str):
        index = int(fid.rsplit("-", 1)[1])
        return FileMeta(
            file_id=fid,
            parent_id=self.file_info_parent_id,
            name=self.names[fid],
            size=7,
            sha1="A" * 40,
            pickcode=f"pc-{index}",
            mtime=0,
            ctime=0,
            is_video=True,
        )

    async def rename_file(self, fid: str, new_name: str):
        self.names[fid] = new_name

    async def delete_files(self, fids, *, pid=None):
        if self.delete_error is not None:
            raise self.delete_error
        return None


def _create_libraries(tmp_path):
    local = MediaLibrary.create(
        name="local",
        backend="local",
        backend_config={"root_path": str(tmp_path)},
    )
    cloud = MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_config={
            "cookies": "UID=1_R2_1; CID=x; SEID=y",
            "root_cid": "root-cid",
            "app": "alipaymini",
        },
    )
    return local, cloud


def _create_local_media(local_library, path: Path, title: str) -> Media:
    path.write_bytes(b"1234567")
    video = VideoItem.create(title=title)
    return Media.create(
        video_item=video,
        library=local_library,
        path=str(path),
        file_size_bytes=path.stat().st_size,
        valid=True,
    )


def _patch_cloud(monkeypatch, client):
    @asynccontextmanager
    async def _fake_client_for(_library):
        yield client

    async def _fake_find_or_create(_client, *, parent_cid, name):
        assert parent_cid == "root-cid"
        assert name == "videos"
        return "videos-cid"

    async def _fake_verify(_client, fid, expected_name):
        assert client.names[fid] == expected_name

    monkeypatch.setattr(rapid_module, "cloud115_client_for", _fake_client_for)
    monkeypatch.setattr(rapid_module, "find_or_create_subdir", _fake_find_or_create)
    monkeypatch.setattr(rapid_module, "verify_cloud115_renamed_file", _fake_verify)
    monkeypatch.setattr(rapid_module.MediaRapidUploadRunner, "submit", lambda *args, **kwargs: None)


def test_batch_rapid_upload_runs_sequentially_and_relocates_media(
    test_db, tmp_path, monkeypatch
):
    local, cloud = _create_libraries(tmp_path)
    first = _create_local_media(local, tmp_path / "first.mp4", "first")
    second = _create_local_media(local, tmp_path / "second.mp4", "second")
    stub = _StubRapidUploadClient(
        statuses=[RapidUploadStatus.SUCCESS, RapidUploadStatus.SUCCESS]
    )
    _patch_cloud(monkeypatch, stub)

    trigger = MediaRapidUploadService.trigger_batch(
        media_ids=[first.id, second.id],
        target_library_id=cloud.id,
    )
    MediaRapidUploadService._run_batch(
        trigger.rapid_upload_batch_id,
        trigger.task_run_id,
    )

    assert stub.calls == ["first.mp4", "second.mp4"]
    assert not (tmp_path / "first.mp4").exists()
    assert not (tmp_path / "second.mp4").exists()
    for media_id in (first.id, second.id):
        media = Media.get_by_id(media_id)
        assert media.library_id == cloud.id
        assert media.path is None
        assert media.storage_mode == "rapid_upload"
        assert media.content_fingerprint == f"sha1:{'A' * 40}"
    batch = MediaRapidUploadBatch.get_by_id(trigger.rapid_upload_batch_id)
    assert batch.succeeded_count == 2
    assert batch.failed_count == 0
    assert SystemNotification.get().category == "info"


def test_batch_not_hit_marks_item_failed_and_keeps_local_media(
    test_db, tmp_path, monkeypatch
):
    local, cloud = _create_libraries(tmp_path)
    media = _create_local_media(local, tmp_path / "not-hit.mp4", "not-hit")
    stub = _StubRapidUploadClient(statuses=[RapidUploadStatus.NOT_HIT])
    _patch_cloud(monkeypatch, stub)

    trigger = MediaRapidUploadService.trigger_batch(
        media_ids=[media.id],
        target_library_id=cloud.id,
    )
    MediaRapidUploadService._run_batch(
        trigger.rapid_upload_batch_id,
        trigger.task_run_id,
    )

    item = MediaRapidUploadItem.get()
    assert item.state == ITEM_STATE_FAILED
    assert item.error_message == "秒传失败"
    assert Path(media.path).exists()
    assert Media.get_by_id(media.id).library_id == local.id
    assert SystemNotification.get().category == "warning"


def test_retry_batch_retries_cleanup_without_uploading_again(
    test_db, tmp_path, monkeypatch
):
    local, cloud = _create_libraries(tmp_path)
    media = _create_local_media(local, tmp_path / "cleanup.mp4", "cleanup")
    stub = _StubRapidUploadClient(statuses=[RapidUploadStatus.SUCCESS])
    _patch_cloud(monkeypatch, stub)
    original_unlink = Path.unlink

    def _deny_unlink(path, *args, **kwargs):
        raise PermissionError("busy")

    monkeypatch.setattr(Path, "unlink", _deny_unlink)
    first = MediaRapidUploadService.trigger_batch(
        media_ids=[media.id], target_library_id=cloud.id
    )
    MediaRapidUploadService._run_batch(first.rapid_upload_batch_id, first.task_run_id)
    batch = MediaRapidUploadBatch.get_by_id(first.rapid_upload_batch_id)
    item = MediaRapidUploadItem.get(MediaRapidUploadItem.batch == batch)
    assert item.state == ITEM_STATE_CLEANUP_FAILED
    assert Path(item.source_path).exists()
    assert Media.get_by_id(media.id).path is None
    with pytest.raises(ApiError) as exc_info:
        MediaService.delete_media(media.id)
    assert exc_info.value.code == "media_rapid_upload_in_progress"

    monkeypatch.setattr(Path, "unlink", original_unlink)

    retry = MediaRapidUploadService.retry_batch(batch.id)
    retry_item = MediaRapidUploadItem.get(
        MediaRapidUploadItem.batch == retry.rapid_upload_batch_id
    )
    assert retry_item.action == ITEM_ACTION_CLEANUP_ONLY
    MediaRapidUploadService._run_batch(retry.rapid_upload_batch_id, retry.task_run_id)

    retry_item = MediaRapidUploadItem.get_by_id(retry_item.id)
    assert retry_item.state == ITEM_STATE_SUCCEEDED
    assert not Path(retry_item.source_path).exists()
    assert stub.calls == ["cleanup.mp4"]


def test_interrupted_remote_upload_is_resumed_without_second_upload(
    test_db, tmp_path, monkeypatch
):
    local, cloud = _create_libraries(tmp_path)
    media = _create_local_media(local, tmp_path / "interrupted.mp4", "interrupted")
    stub = _StubRapidUploadClient(statuses=[])
    _patch_cloud(monkeypatch, stub)

    trigger = MediaRapidUploadService.trigger_batch(
        media_ids=[media.id], target_library_id=cloud.id
    )
    batch = MediaRapidUploadBatch.get_by_id(trigger.rapid_upload_batch_id)
    item = MediaRapidUploadItem.get(MediaRapidUploadItem.batch == batch)
    target_name = f"media-{media.id}＿interrupted.mp4"
    item.state = "remote_uploaded"
    item.source_sha1 = "A" * 40
    item.target_cid = "videos-cid"
    item.target_fid = "fid-1"
    item.target_pickcode = "pc-1"
    item.target_name = target_name
    item.save()
    stub.names["fid-1"] = target_name
    batch.state = "running"
    batch.save()

    result = MediaRapidUploadService.recover_interrupted_batches()
    assert result == {"recovered_count": 1}
    item = MediaRapidUploadItem.get_by_id(item.id)
    assert item.state == ITEM_STATE_FAILED
    # 实际启动恢复会先由 ActivityService 释放任务锁；此处补齐同样前置状态。
    task_run = batch.task_run
    task_run.mutex_key = None
    task_run.save()

    retry = MediaRapidUploadService.retry_batch(batch.id)
    retry_item = MediaRapidUploadItem.get(
        MediaRapidUploadItem.batch == retry.rapid_upload_batch_id
    )
    assert retry_item.action == ITEM_ACTION_RESUME_REMOTE
    MediaRapidUploadService._run_batch(retry.rapid_upload_batch_id, retry.task_run_id)

    assert stub.calls == []
    assert MediaRapidUploadItem.get_by_id(retry_item.id).state == ITEM_STATE_SUCCEEDED
    assert Media.get_by_id(media.id).library_id == cloud.id
    assert not (tmp_path / "interrupted.mp4").exists()


def test_retry_adopts_remote_file_when_rollback_failed(
    test_db, tmp_path, monkeypatch
):
    local, cloud = _create_libraries(tmp_path)
    media = _create_local_media(local, tmp_path / "rollback.mp4", "rollback")
    stub = _StubRapidUploadClient(statuses=[RapidUploadStatus.SUCCESS])
    stub.file_info_parent_id = "wrong-cid"
    stub.delete_error = RuntimeError("115 temporary failure")
    _patch_cloud(monkeypatch, stub)

    first = MediaRapidUploadService.trigger_batch(
        media_ids=[media.id], target_library_id=cloud.id
    )
    MediaRapidUploadService._run_batch(first.rapid_upload_batch_id, first.task_run_id)

    first_item = MediaRapidUploadItem.get(
        MediaRapidUploadItem.batch == first.rapid_upload_batch_id
    )
    assert first_item.state == ITEM_STATE_FAILED
    assert first_item.target_fid == "fid-1"
    assert first_item.target_pickcode == "pc-1"
    assert first_item.target_name == f"media-{media.id}＿rollback.mp4"
    assert Path(first_item.source_path).exists()

    stub.file_info_parent_id = "videos-cid"
    stub.delete_error = None
    retry = MediaRapidUploadService.retry_batch(first.rapid_upload_batch_id)
    retry_item = MediaRapidUploadItem.get(
        MediaRapidUploadItem.batch == retry.rapid_upload_batch_id
    )
    assert retry_item.action == ITEM_ACTION_RESUME_REMOTE
    MediaRapidUploadService._run_batch(retry.rapid_upload_batch_id, retry.task_run_id)

    assert stub.calls == ["rollback.mp4"]
    assert MediaRapidUploadItem.get_by_id(retry_item.id).state == ITEM_STATE_SUCCEEDED
    assert Media.get_by_id(media.id).library_id == cloud.id
    assert not (tmp_path / "rollback.mp4").exists()


def test_startup_recovery_sends_only_batch_completion_notification(
    test_db, tmp_path, monkeypatch
):
    from src.start.recovery import recover_interrupted_tasks

    local, cloud = _create_libraries(tmp_path)
    media = _create_local_media(local, tmp_path / "recovery.mp4", "recovery")
    _patch_cloud(monkeypatch, _StubRapidUploadClient(statuses=[]))
    trigger = MediaRapidUploadService.trigger_batch(
        media_ids=[media.id], target_library_id=cloud.id
    )
    batch = MediaRapidUploadBatch.get_by_id(trigger.rapid_upload_batch_id)
    batch.state = "running"
    batch.save()

    recovered = recover_interrupted_tasks(
        trigger_types=("manual",), error_message="服务重启，任务已中断"
    )

    assert recovered == {"media_rapid_upload"}
    assert BackgroundTaskRun.get_by_id(trigger.task_run_id).state == "failed"
    batch = MediaRapidUploadBatch.get_by_id(batch.id)
    assert batch.state == "failed"
    notifications = list(SystemNotification.select())
    assert len(notifications) == 1
    assert notifications[0].related_task_run_id == trigger.task_run_id
    assert notifications[0].related_resource_type == "media_rapid_upload_batch"
    assert notifications[0].related_resource_id == batch.id
    assert batch.completion_notification_id == notifications[0].id


def test_delete_library_rejects_when_rapid_upload_history_exists(
    test_db, tmp_path, monkeypatch
):
    local, cloud = _create_libraries(tmp_path)
    media = _create_local_media(local, tmp_path / "history.mp4", "history")
    _patch_cloud(monkeypatch, _StubRapidUploadClient(statuses=[RapidUploadStatus.NOT_HIT]))
    trigger = MediaRapidUploadService.trigger_batch(
        media_ids=[media.id], target_library_id=cloud.id
    )
    MediaRapidUploadService._run_batch(trigger.rapid_upload_batch_id, trigger.task_run_id)

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.delete_library(cloud.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_in_use"
