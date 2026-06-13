import pytest

from src.config.config import settings
from src.model import MediaLibrary, VideoImportJob
from src.service.transfers.import_runner import DownloadImportRunner


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
    library = MediaLibrary.create(name="Videos", root_path=str(tmp_path / "library"))
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
    library = MediaLibrary.create(name="Videos", root_path=str(tmp_path / "library"))
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
