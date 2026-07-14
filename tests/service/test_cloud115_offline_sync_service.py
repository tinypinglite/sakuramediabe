"""Cloud115OfflineSyncService（离线任务对账 / 自动导入 / 超时放弃）的测试。"""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.lib.cloud115 import OfflineTask
from src.model import (
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    ImportJob,
    MediaLibrary,
    SystemEvent,
    SystemNotification,
)
from src.schema.transfers.media_import import ImportJobTriggerResponse
from src.service.transfers import cloud115_offline_sync_service as sync_module
from src.service.transfers.cloud115_offline_sync_service import Cloud115OfflineSyncService

COOKIES = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz"


@pytest.fixture()
def offline_sync_tables(test_db):
    models = [
        MediaLibrary,
        DownloadClient,
        DownloadTask,
        BackgroundTaskRun,
        ImportJob,
        SystemNotification,
        SystemEvent,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db


def _create_client() -> DownloadClient:
    library = MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_config={
            "cookies": COOKIES,
            "root_cid": "100",
            "download_root_cid": "dl-root",
        },
    )
    return DownloadClient.create(name="cloud115-main", kind="cloud115", media_library=library)


def _create_task(client: DownloadClient, **overrides) -> DownloadTask:
    payload = {
        "client": client,
        "movie": "ABP-123",
        "name": "ABP-123",
        "info_hash": "a" * 40,
        "save_path": "sakuramedia_downloads/ABP-123",
        "target_ref": {"cid": "cid-ABP-123"},
        "download_state": "downloading",
        "import_status": "pending",
    }
    payload.update(overrides)
    return DownloadTask.create(**payload)


def _remote_task(info_hash: str, *, status: int, percent: float = 0.0, name: str = "") -> OfflineTask:
    return OfflineTask(
        info_hash=info_hash,
        name=name or "ABP-123",
        size=1024,
        status=status,
        status_text="",
        percent_done=percent,
        rate_download=0,
        peers=0,
        left_time_seconds=0,
        add_time=0,
        last_update=0,
        file_id="",
        pickcode="",
        save_dir_id="cid-ABP-123",
        source_url="",
        retry_count=0,
        retry_limit=0,
    )


class _FakeTaskPage:
    def __init__(self, tasks):
        self.page = 1
        self.page_count = 1
        self.page_size = 50
        self.total_tasks = len(tasks)
        self.tasks = tasks


@pytest.fixture()
def patched_sdk(monkeypatch):
    """替换 cloud115_client_for，list_offline_tasks 返回预设远端任务；记录调用次数。"""

    state = {"calls": 0, "tasks": []}

    class _FakeSdkClient:
        async def list_offline_tasks(self, *, page: int = 1, page_size: int = 30):
            state["calls"] += 1
            return _FakeTaskPage(state["tasks"] if page == 1 else [])

    @asynccontextmanager
    async def _fake_client_for(library):
        yield _FakeSdkClient()

    monkeypatch.setattr(sync_module, "cloud115_client_for", _fake_client_for)
    return state


def test_sync_client_applies_remote_progress_and_state(offline_sync_tables, patched_sdk):
    client = _create_client()
    task = _create_task(client)
    patched_sdk["tasks"] = [_remote_task("a" * 40, status=1, percent=42.5)]

    summary = Cloud115OfflineSyncService().sync_client(client)

    assert summary["updated_count"] == 1
    task = DownloadTask.get_by_id(task.id)
    assert task.download_state == "downloading"
    assert task.progress == pytest.approx(0.425)


def test_sync_client_matches_remote_hash_after_canonicalization(
    offline_sync_tables, patched_sdk
):
    client = _create_client()
    task = _create_task(client)
    patched_sdk["tasks"] = [_remote_task("A" * 40, status=1, percent=25.0)]

    summary = Cloud115OfflineSyncService().sync_client(client)

    assert summary["updated_count"] == 1
    assert DownloadTask.get_by_id(task.id).progress == pytest.approx(0.25)


def test_sync_client_triggers_import_when_remote_completed(
    offline_sync_tables, patched_sdk, monkeypatch
):
    client = _create_client()
    task = _create_task(client)
    patched_sdk["tasks"] = [_remote_task("a" * 40, status=2, percent=100.0)]
    library = client.media_library
    job = ImportJob.create(source_path="cloud", source_cid="cid-ABP-123", library=library)

    trigger_calls = []

    def _fake_trigger(library_id, source_cid, transfer_mode="cleanup-source"):
        trigger_calls.append((library_id, source_cid, transfer_mode))
        return ImportJobTriggerResponse(import_job_id=job.id, task_run_id=1, status="accepted")

    monkeypatch.setattr(
        sync_module.Cloud115ImportJobService,
        "trigger_cloud115_import",
        staticmethod(_fake_trigger),
    )

    summary = Cloud115OfflineSyncService().sync_client(client)

    assert summary["import_triggered_count"] == 1
    assert trigger_calls == [(library.id, "cid-ABP-123", "cleanup-source")]
    task = DownloadTask.get_by_id(task.id)
    assert task.download_state == "completed"
    assert task.import_status == "running"
    # ImportJob 已关联回下载任务（状态对账与孤儿恢复靠这条链）。
    assert ImportJob.get_by_id(job.id).download_task_id == task.id


def test_sync_client_keeps_pending_on_import_mutex_conflict(
    offline_sync_tables, patched_sdk, monkeypatch
):
    client = _create_client()
    task = _create_task(client)
    patched_sdk["tasks"] = [_remote_task("a" * 40, status=2, percent=100.0)]

    def _fake_trigger(library_id, source_cid, transfer_mode="cleanup-source"):
        raise ApiError(409, "media_import_conflict", "已有导入在跑")

    monkeypatch.setattr(
        sync_module.Cloud115ImportJobService,
        "trigger_cloud115_import",
        staticmethod(_fake_trigger),
    )

    summary = Cloud115OfflineSyncService().sync_client(client)

    assert summary["import_triggered_count"] == 0
    task = DownloadTask.get_by_id(task.id)
    assert task.import_status == "pending"  # 下一轮对账重试


def test_sync_client_reconciles_running_import_from_job_state(
    offline_sync_tables, patched_sdk
):
    client = _create_client()
    task = _create_task(client, download_state="completed", import_status="running")
    ImportJob.create(
        source_path="cloud",
        source_cid="cid-ABP-123",
        library=client.media_library,
        download_task=task,
        state="completed",
        failed_count=0,
    )
    patched_sdk["tasks"] = [_remote_task("a" * 40, status=2, percent=100.0)]

    Cloud115OfflineSyncService().sync_client(client)

    assert DownloadTask.get_by_id(task.id).import_status == "completed"
    assert patched_sdk["calls"] == 0


def test_sync_client_marks_import_failed_when_job_has_failures(
    offline_sync_tables, patched_sdk
):
    client = _create_client()
    task = _create_task(client, download_state="completed", import_status="running")
    ImportJob.create(
        source_path="cloud",
        source_cid="cid-ABP-123",
        library=client.media_library,
        download_task=task,
        state="completed",
        failed_count=2,
    )
    patched_sdk["tasks"] = [_remote_task("a" * 40, status=2, percent=100.0)]

    Cloud115OfflineSyncService().sync_client(client)

    assert DownloadTask.get_by_id(task.id).import_status == "failed"
    assert patched_sdk["calls"] == 0


def test_sync_client_does_not_poll_or_abandon_failed_task(offline_sync_tables, patched_sdk):
    client = _create_client()
    task = _create_task(client, download_state="failed")
    DownloadTask.update(created_at=utc_now_for_db() - timedelta(hours=48)).where(
        DownloadTask.id == task.id
    ).execute()

    summary = Cloud115OfflineSyncService().sync_client(client)

    assert summary["abandoned_count"] == 0
    assert DownloadTask.get_by_id(task.id).download_state == "failed"
    assert patched_sdk["calls"] == 0


def test_sync_client_keeps_remote_failure_in_failed_state(
    offline_sync_tables, patched_sdk
):
    client = _create_client()
    task = _create_task(client)
    DownloadTask.update(created_at=utc_now_for_db() - timedelta(hours=48)).where(
        DownloadTask.id == task.id
    ).execute()
    patched_sdk["tasks"] = [_remote_task(task.info_hash, status=-1)]

    summary = Cloud115OfflineSyncService().sync_client(client)

    assert summary["updated_count"] == 1
    assert summary["abandoned_count"] == 0
    assert DownloadTask.get_by_id(task.id).download_state == "failed"


def test_sync_client_abandons_timed_out_task_and_notifies(offline_sync_tables, patched_sdk):
    client = _create_client()
    task = _create_task(client)
    # 提交时间拨回 25 小时前（默认阈值 24h）。
    DownloadTask.update(
        created_at=utc_now_for_db() - timedelta(hours=25)
    ).where(DownloadTask.id == task.id).execute()
    patched_sdk["tasks"] = [_remote_task("a" * 40, status=1, percent=10.0)]

    summary = Cloud115OfflineSyncService().sync_client(client)

    assert summary["abandoned_count"] == 1
    task = DownloadTask.get_by_id(task.id)
    assert task.download_state == "abandoned"
    # 发出了放弃通知。
    assert (
        SystemNotification.select()
        .where(SystemNotification.title.contains("超时已放弃"))
        .exists()
    )


def test_sync_client_skips_abandoned_tasks_without_sdk_call(offline_sync_tables, patched_sdk):
    client = _create_client()
    _create_task(client, download_state="abandoned")

    summary = Cloud115OfflineSyncService().sync_client(client)

    assert summary == {"updated_count": 0, "import_triggered_count": 0, "abandoned_count": 0}
    assert patched_sdk["calls"] == 0  # 全部放弃时零请求


def test_sync_client_leaves_state_untouched_when_remote_missing(
    offline_sync_tables, patched_sdk
):
    # 远端列表没拉到该 info_hash：分页上限内可能没拉全，不能误判为删除。
    client = _create_client()
    task = _create_task(client)
    patched_sdk["tasks"] = []

    summary = Cloud115OfflineSyncService().sync_client(client)

    assert summary["updated_count"] == 0
    assert DownloadTask.get_by_id(task.id).download_state == "downloading"


def test_run_iterates_only_cloud115_clients(offline_sync_tables, patched_sdk):
    local_library = MediaLibrary.create(
        name="local", backend="local", backend_config={"root_path": "/library"}
    )
    DownloadClient.create(
        name="qb-main",
        kind="qbittorrent",
        base_url="http://qb:8080",
        username="admin",
        password="secret",
        client_save_path="/downloads",
        local_root_path="/mnt/downloads",
        media_library=local_library,
    )
    cloud_client = _create_client()
    _create_task(cloud_client)
    patched_sdk["tasks"] = [_remote_task("a" * 40, status=1, percent=50.0)]

    stats = Cloud115OfflineSyncService().run()

    assert stats["total_clients"] == 1  # 只有 cloud115 kind 参与对账
    assert stats["updated_count"] == 1
    assert stats["failed_count"] == 0


def test_recover_interrupted_import_deletes_job_resets_task_and_keeps_activity(
    offline_sync_tables,
):
    client = _create_client()
    task = _create_task(client, download_state="completed", import_status="running")
    task_run = BackgroundTaskRun.create(
        task_key="media_directory_import",
        task_name="115 导入",
        trigger_type="manual",
        state="failed",
        error_message="进程中断",
    )
    job = ImportJob.create(
        source_path="cloud115:cid-ABP-123",
        source_cid="cid-ABP-123",
        library=client.media_library,
        download_task=task,
        task_run=task_run,
        state="running",
        transfer_mode="cleanup-source",
    )

    assert Cloud115OfflineSyncService.recover_interrupted_imports() == 1
    assert ImportJob.get_or_none(ImportJob.id == job.id) is None
    assert DownloadTask.get_by_id(task.id).import_status == "pending"
    kept_activity = BackgroundTaskRun.get_by_id(task_run.id)
    assert kept_activity.state == "failed"
    assert kept_activity.error_message == "进程中断"
    # API/APS 双启动重复调用时保持幂等。
    assert Cloud115OfflineSyncService.recover_interrupted_imports() == 0


def test_recovered_import_is_triggered_only_once_on_next_sync(
    offline_sync_tables, patched_sdk, monkeypatch
):
    client = _create_client()
    task = _create_task(client, download_state="completed", import_status="running")
    task_run = BackgroundTaskRun.create(
        task_key="media_directory_import",
        task_name="115 导入",
        trigger_type="manual",
        state="failed",
    )
    ImportJob.create(
        source_path="cloud115:cid-ABP-123",
        source_cid="cid-ABP-123",
        library=client.media_library,
        download_task=task,
        task_run=task_run,
        state="pending",
        transfer_mode="cleanup-source",
    )
    assert Cloud115OfflineSyncService.recover_interrupted_imports() == 1

    calls = []

    def _fake_trigger(library_id, source_cid, transfer_mode="cleanup-source"):
        calls.append((library_id, source_cid, transfer_mode))
        new_run = BackgroundTaskRun.create(
            task_key="media_directory_import",
            task_name="115 重试",
            trigger_type="scheduled",
            state="running",
        )
        new_job = ImportJob.create(
            source_path=f"cloud115:{source_cid}",
            source_cid=source_cid,
            library=library_id,
            task_run=new_run,
            state="pending",
            transfer_mode=transfer_mode,
        )
        return ImportJobTriggerResponse(
            import_job_id=new_job.id, task_run_id=new_run.id, status="accepted"
        )

    monkeypatch.setattr(
        sync_module.Cloud115ImportJobService,
        "trigger_cloud115_import",
        staticmethod(_fake_trigger),
    )

    service = Cloud115OfflineSyncService()
    first = service.sync_client(client)
    second = service.sync_client(client)

    assert first["import_triggered_count"] == 1
    assert second["import_triggered_count"] == 0
    assert len(calls) == 1
    assert patched_sdk["calls"] == 0
