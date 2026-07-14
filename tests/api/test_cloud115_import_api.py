"""cloud115 导入作业 API 测试：POST /import-jobs 分派、重导、失败文件操作限制。

115 侧校验（防御校验 + 源目录名）与后台执行分别用 monkeypatch stub 掉，
只验证接口行为与 ImportJob 落库形状。
"""

from __future__ import annotations

import pytest

from src.model import BackgroundTaskRun, ImportJob, MediaLibrary
from src.service.transfers.cloud115_import_job_service import Cloud115ImportJobService
from src.service.transfers.import_runner import DownloadImportRunner

COOKIES = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz"


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


@pytest.fixture()
def stub_cloud115_side(monkeypatch):
    """跳过打真实 115 的触发校验，并把后台 runner 置空。"""
    monkeypatch.setattr(
        Cloud115ImportJobService,
        "_validate_source_and_fetch_name",
        staticmethod(lambda library, source_cid: "来源目录"),
    )
    monkeypatch.setattr(DownloadImportRunner, "submit", staticmethod(lambda *a, **k: None))


def _create_cloud_library(name="cloud") -> MediaLibrary:
    return MediaLibrary.create(
        name=name,
        backend="cloud115",
        backend_config={"cookies": COOKIES, "root_cid": "lib-root", "app": "alipaymini"},
    )


def test_create_cloud115_import_job(client, account_user, stub_cloud115_side):
    library = _create_cloud_library()
    token = _login(client, username=account_user.username)

    response = client.post(
        "/import-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={"library_id": library.id, "source_cid": "src-1", "transfer_mode": "move"},
    )

    assert response.status_code == 202
    body = response.json()
    job = ImportJob.get_by_id(body["import_job_id"])
    assert job.source_cid == "src-1"
    assert job.transfer_mode == "cleanup-source"
    assert job.library_id == library.id
    assert job.download_task_id is None


def test_create_cloud115_import_job_defaults_to_cleanup_source(
    client, account_user, stub_cloud115_side
):
    library = _create_cloud_library()
    token = _login(client, username=account_user.username)

    response = client.post(
        "/import-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={"library_id": library.id, "source_cid": "src-1"},
    )

    assert response.status_code == 202
    job = ImportJob.get_by_id(response.json()["import_job_id"])
    assert job.transfer_mode == "cleanup-source"


def test_create_import_job_rejects_both_sources(client, account_user):
    token = _login(client, username=account_user.username)
    response = client.post(
        "/import-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={"library_id": 1, "source_path": "/mnt/x", "source_cid": "src-1"},
    )
    assert response.status_code == 422


def test_create_import_job_rejects_neither_source(client, account_user):
    token = _login(client, username=account_user.username)
    response = client.post(
        "/import-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={"library_id": 1},
    )
    assert response.status_code == 422


def test_create_cloud115_import_job_rejects_invalid_transfer_mode(
    client, account_user, stub_cloud115_side
):
    library = _create_cloud_library()
    token = _login(client, username=account_user.username)
    response = client.post(
        "/import-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={"library_id": library.id, "source_cid": "src-1", "transfer_mode": "auto"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_transfer_mode"


def test_create_cloud115_import_job_rejects_local_library(
    client, account_user, stub_cloud115_side
):
    library = MediaLibrary.create(
        name="local", backend="local", backend_config={"root_path": "/library/a"}
    )
    token = _login(client, username=account_user.username)
    response = client.post(
        "/import-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={"library_id": library.id, "source_cid": "src-1"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_library_backend_mismatch"


def test_create_cloud115_import_job_conflicts_on_same_library(
    client, account_user, stub_cloud115_side
):
    """mutex 按库锁：同一 cloud115 库并发第二个导入任务应 409（源目录不同也一样）。"""
    library = _create_cloud_library()
    token = _login(client, username=account_user.username)
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post(
        "/import-jobs", headers=headers,
        json={"library_id": library.id, "source_cid": "src-1"},
    )
    assert first.status_code == 202
    second = client.post(
        "/import-jobs", headers=headers,
        json={"library_id": library.id, "source_cid": "src-other"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "media_import_conflict"


def test_cloud115_job_failed_file_delete_and_rename_rejected(
    client, account_user, stub_cloud115_side
):
    library = _create_cloud_library()
    job = ImportJob.create(
        source_path="cloud115:src-1",
        source_cid="src-1",
        library=library,
        state="failed",
        transfer_mode="move",
        failed_count=1,
        failed_files='[{"path": "A/movie.mp4", "reason": "movie_number_not_found", "detail": "", "kind": "file"}]',
    )
    token = _login(client, username=account_user.username)
    headers = {"Authorization": f"Bearer {token}"}

    delete_response = client.request(
        "DELETE", f"/import-jobs/{job.id}/failed-files",
        headers=headers, json={"path": "A/movie.mp4"},
    )
    rename_response = client.post(
        f"/import-jobs/{job.id}/failed-files/rename",
        headers=headers, json={"path": "A/movie.mp4", "new_name": "B.mp4"},
    )

    assert delete_response.status_code == 422
    assert delete_response.json()["error"]["code"] == "cloud115_failed_file_not_actionable"
    assert rename_response.status_code == 422
    assert rename_response.json()["error"]["code"] == "cloud115_failed_file_not_actionable"


def test_cloud115_job_retry_uses_relative_paths(client, account_user, stub_cloud115_side):
    library = _create_cloud_library()
    job = ImportJob.create(
        source_path="cloud115:src-1",
        source_cid="src-1",
        library=library,
        state="failed",
        transfer_mode="copy",
        failed_count=1,
        failed_files='[{"path": "A/movie.mp4", "reason": "cloud115_transfer_failed", "detail": "", "kind": "file"}]',
    )
    token = _login(client, username=account_user.username)

    response = client.post(
        f"/import-jobs/{job.id}/retry",
        headers={"Authorization": f"Bearer {token}"},
        json={"files": ["A/movie.mp4"]},
    )

    assert response.status_code == 200
    retry_job = ImportJob.get_by_id(response.json()["import_job_id"])
    assert retry_job.id != job.id
    assert retry_job.source_cid == "src-1"
    # 重导沿用原作业的导入模式
    assert retry_job.transfer_mode == "copy"
    task_run = BackgroundTaskRun.select().order_by(BackgroundTaskRun.id.desc()).get()
    assert task_run.mutex_key == f"media_import:cloud115:{library.id}"


def test_cloud115_job_retry_rejects_path_outside_failed_list(
    client, account_user, stub_cloud115_side
):
    library = _create_cloud_library()
    job = ImportJob.create(
        source_path="cloud115:src-1",
        source_cid="src-1",
        library=library,
        state="failed",
        transfer_mode="move",
        failed_files='[{"path": "A/movie.mp4", "reason": "cloud115_transfer_failed", "detail": "", "kind": "file"}]',
    )
    token = _login(client, username=account_user.username)

    response = client.post(
        f"/import-jobs/{job.id}/retry",
        headers={"Authorization": f"Bearer {token}"},
        json={"files": ["B/other.mp4"]},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_not_in_failed_list"


def test_cloud115_job_appears_in_job_list_with_source_cid(
    client, account_user, stub_cloud115_side
):
    library = _create_cloud_library()
    ImportJob.create(
        source_path="根目录/来源目录",
        source_cid="src-1",
        library=library,
        state="completed",
        transfer_mode="move",
    )
    token = _login(client, username=account_user.username)

    response = client.get(
        "/import-jobs", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["source_cid"] == "src-1"
    assert items[0]["source_path"] == "根目录/来源目录"
