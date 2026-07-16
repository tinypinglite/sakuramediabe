import json

import pytest

from src.config.config import settings
from src.model import MediaLibrary, VideoImportJob
from src.service.transfers.import_runner import DownloadImportRunner
from src.service.videos import Cloud115VideoImportJobService


def _login(client, username):
    response = client.post("/auth/tokens", json={"username": username, "password": "password123"})
    return response.json()["access_token"]


@pytest.fixture(autouse=True)
def allow_tmp_browse_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(settings.media_import, "browse_roots", [str(tmp_path)])


@pytest.fixture(autouse=True)
def stub_runner_submit(monkeypatch):
    monkeypatch.setattr(DownloadImportRunner, "submit", staticmethod(lambda *a, **k: None))


def test_video_import_requires_authentication(client):
    response = client.post("/video-imports", json={"library_id": 1, "source_path": "/tmp"})
    assert response.status_code == 401


def test_trigger_video_import_returns_202(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    library = MediaLibrary.create(name="Videos", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    source = tmp_path / "incoming"
    source.mkdir()

    response = client.post(
        "/video-imports",
        json={"library_id": library.id, "source_path": str(source), "transfer_mode": "auto"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["video_import_job_id"] > 0
    job = VideoImportJob.get_by_id(body["video_import_job_id"])
    assert job.state == "pending"


def test_trigger_cloud115_video_import_dispatches_with_copy_default(
    client, account_user, monkeypatch
):
    token = _login(client, account_user.username)
    captured = {}

    def fake_trigger(library_id, **kwargs):
        captured.update({"library_id": library_id, **kwargs})
        return {"video_import_job_id": 7, "task_run_id": 8, "status": "accepted"}

    monkeypatch.setattr(
        Cloud115VideoImportJobService,
        "trigger_cloud115_import",
        staticmethod(fake_trigger),
    )
    response = client.post(
        "/video-imports",
        json={"library_id": 2, "source_cid": "cid-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202
    assert response.json()["video_import_job_id"] == 7
    assert captured == {
        "library_id": 2,
        "source_cid": "cid-1",
        "source_fid": None,
        "transfer_mode": "copy",
        "collection_id": None,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"library_id": 1},
        {"library_id": 1, "source_path": "/tmp", "source_cid": "cid"},
        {"library_id": 1, "source_cid": "cid", "source_fid": "fid"},
    ],
)
def test_trigger_video_import_requires_exactly_one_source(
    client, account_user, payload
):
    token = _login(client, account_user.username)
    response = client.post(
        "/video-imports",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_trigger_video_import_rejects_cloud115_library(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    library = MediaLibrary.create(
        name="Cloud Videos",
        backend="cloud115",
        backend_account_key="cloud115:video-import-mismatch",
        backend_config={"cookies": "UID=x_A1_y", "root_cid": "root", "app": "web"},
    )
    source = tmp_path / "incoming-cloud"
    source.mkdir()

    response = client.post(
        "/video-imports",
        json={"library_id": library.id, "source_path": str(source), "transfer_mode": "auto"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_library_backend_mismatch"
    assert VideoImportJob.select().count() == 0


def test_trigger_video_import_missing_library_is_422(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    response = client.post(
        "/video-imports",
        json={"source_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {token}"},
    )
    # library_id 为必填，schema 校验返回 422。
    assert response.status_code == 422


def test_get_video_import_job(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    library = MediaLibrary.create(name="Videos", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    job = VideoImportJob.create(source_path=str(tmp_path), library=library, state="completed", imported_count=3)

    response = client.get(
        f"/video-imports/{job.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == job.id
    assert body["state"] == "completed"
    assert body["imported_count"] == 3
    assert body["failed_files"] == []


def test_get_cloud_video_import_job_returns_source_ids(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    library = MediaLibrary.create(
        name="Cloud Videos",
        backend="cloud115",
        backend_account_key="cloud115:api-video-job",
        backend_config={"cookies": "UID=x_A1_y", "root_cid": "root", "app": "web"},
    )
    job = VideoImportJob.create(
        source_path="根目录/来源",
        source_cid="cid-1",
        library=library,
        state="completed",
    )
    response = client.get(
        f"/video-imports/{job.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["source_cid"] == "cid-1"
    assert response.json()["source_fid"] is None


def test_list_video_import_jobs(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    library = MediaLibrary.create(name="Videos", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    older = VideoImportJob.create(source_path=str(tmp_path / "a"), library=library, state="completed")
    newer = VideoImportJob.create(source_path=str(tmp_path / "b"), library=library, state="failed")

    response = client.get("/video-imports", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    # 按 id 倒序：最新作业在前。
    assert [item["id"] for item in body["items"]] == [newer.id, older.id]


def test_retry_video_import_failed_files_returns_202(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    library = MediaLibrary.create(name="Videos", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    source = tmp_path / "incoming"
    source.mkdir()
    failed_file = source / "clip.mp4"
    failed_file.write_bytes(b"data")
    job = VideoImportJob.create(
        source_path=str(source),
        library=library,
        state="failed",
        failed_count=1,
        failed_files=json.dumps(
            [{"path": str(failed_file), "reason": "media_import_failed", "detail": "", "kind": "file"}]
        ),
    )

    response = client.post(
        f"/video-imports/{job.id}/retry",
        json={"files": [str(failed_file)]},
        headers={"Authorization": f"Bearer {token}"},
    )

    # 重导与 JAV import-jobs/retry 一致，返回 200（路由未声明 202）。
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    # 重导会新建一个导入作业。
    assert body["video_import_job_id"] != job.id


def test_retry_rejects_path_not_in_failed_list(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    library = MediaLibrary.create(name="Videos", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    job = VideoImportJob.create(
        source_path=str(tmp_path),
        library=library,
        state="failed",
        failed_count=0,
        failed_files="[]",
    )

    response = client.post(
        f"/video-imports/{job.id}/retry",
        json={"files": [str(tmp_path / "ghost.mp4")]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_delete_video_import_failed_file(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    library = MediaLibrary.create(name="Videos", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    source = tmp_path / "incoming"
    source.mkdir()
    failed_file = source / "clip.mp4"
    failed_file.write_bytes(b"data")
    job = VideoImportJob.create(
        source_path=str(source),
        library=library,
        state="failed",
        failed_count=1,
        failed_files=json.dumps(
            [{"path": str(failed_file), "reason": "media_import_failed", "detail": "", "kind": "file"}]
        ),
    )

    response = client.request(
        "DELETE",
        f"/video-imports/{job.id}/failed-files",
        json={"path": str(failed_file)},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["failed_files"] == []
    assert not failed_file.exists()


def test_rename_video_import_failed_file(client, account_user, tmp_path):
    token = _login(client, account_user.username)
    library = MediaLibrary.create(name="Videos", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    source = tmp_path / "incoming"
    source.mkdir()
    failed_file = source / "raw.mp4"
    failed_file.write_bytes(b"data")
    job = VideoImportJob.create(
        source_path=str(source),
        library=library,
        state="failed",
        failed_count=1,
        failed_files=json.dumps(
            [{"path": str(failed_file), "reason": "media_import_failed", "detail": "", "kind": "file"}]
        ),
    )

    response = client.post(
        f"/video-imports/{job.id}/failed-files/rename",
        json={"path": str(failed_file), "new_name": "clip.mp4"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    renamed = source / "clip.mp4"
    assert renamed.exists()
    assert not failed_file.exists()
    assert response.json()["failed_files"][0]["path"] == str(renamed)
