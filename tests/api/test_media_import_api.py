import json

import pytest

from src.model import ImportJob, MediaLibrary
from src.service.transfers.import_runner import DownloadImportRunner


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


@pytest.fixture()
def stub_runner_submit(monkeypatch):
    # API 层只验证接口行为，后台导入执行用空实现替换。
    monkeypatch.setattr(DownloadImportRunner, "submit", staticmethod(lambda *a, **k: None))


def _create_library(tmp_path) -> MediaLibrary:
    return MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))


def test_media_import_endpoints_require_authentication(client):
    responses = [
        client.get("/filesystem/entries"),
        client.post("/import-jobs", json={"library_id": 1, "source_path": "/tmp"}),
        client.get("/import-jobs"),
        client.get("/import-jobs/1"),
        client.post("/import-jobs/1/retry", json={}),
        client.request("DELETE", "/import-jobs/1/failed-files", json={"path": "/tmp/x"}),
        client.post("/import-jobs/1/failed-files/rename", json={"path": "/tmp/x", "new_name": "y"}),
    ]

    for response in responses:
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_browse_filesystem_returns_dirs_and_videos(client, account_user, tmp_path):
    token = _login(client, username=account_user.username)
    browse_root = tmp_path / "incoming"
    browse_root.mkdir()
    (browse_root / "sub").mkdir()
    (browse_root / "ABP-123.mp4").write_bytes(b"video")
    (browse_root / "note.txt").write_text("not a video", encoding="utf-8")

    response = client.get(
        "/filesystem/entries",
        params={"path": str(browse_root)},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == str(browse_root.resolve())
    names = {entry["name"]: entry for entry in body["entries"]}
    assert names["sub"]["type"] == "dir"
    assert names["ABP-123.mp4"]["type"] == "video"
    assert names["ABP-123.mp4"]["is_video"] is True
    # 非视频文件不返回。
    assert "note.txt" not in names


def test_browse_filesystem_rejects_blacklisted_path(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/filesystem/entries",
        params={"path": "/etc"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "path_forbidden"


def test_create_import_job_accepts_and_creates_records(client, account_user, tmp_path, stub_runner_submit):
    token = _login(client, username=account_user.username)
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()

    response = client.post(
        "/import-jobs",
        json={"library_id": library.id, "source_path": str(source_dir)},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert ImportJob.get_by_id(body["import_job_id"]).state == "pending"


def test_create_import_job_conflict_returns_409(client, account_user, tmp_path, stub_runner_submit):
    token = _login(client, username=account_user.username)
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"library_id": library.id, "source_path": str(source_dir)}

    first = client.post("/import-jobs", json=payload, headers=headers)
    second = client.post("/import-jobs", json=payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "media_import_conflict"


def test_list_and_get_import_job(client, account_user, tmp_path):
    token = _login(client, username=account_user.username)
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    job = ImportJob.create(
        source_path=str(source_dir),
        library=library,
        state="failed",
        failed_count=1,
        failed_files=json.dumps(
            [{"path": str(source_dir / "ABP-123.mp4"), "reason": "metadata_fetch_failed", "detail": "boom"}],
            ensure_ascii=False,
        ),
    )
    headers = {"Authorization": f"Bearer {token}"}

    list_response = client.get("/import-jobs", headers=headers)
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1

    detail_response = client.get(f"/import-jobs/{job.id}", headers=headers)
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["failed_files"][0]["reason"] == "metadata_fetch_failed"
    assert detail["failed_files"][0]["detail"] == "boom"


def test_retry_import_job_failed_files(client, account_user, tmp_path, stub_runner_submit):
    token = _login(client, username=account_user.username)
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    failed_path = str(source_dir / "ABP-123.mp4")
    job = ImportJob.create(
        source_path=str(source_dir),
        library=library,
        state="failed",
        failed_count=1,
        failed_files=json.dumps(
            [{"path": failed_path, "reason": "metadata_fetch_failed", "detail": ""}],
            ensure_ascii=False,
        ),
    )

    response = client.post(
        f"/import-jobs/{job.id}/retry",
        json={"files": [failed_path]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["import_job_id"] != job.id


def test_delete_and_rename_failed_files(client, account_user, tmp_path):
    token = _login(client, username=account_user.username)
    library = _create_library(tmp_path)
    source_dir = tmp_path / "incoming"
    source_dir.mkdir()
    rename_target = source_dir / "RENAME.mp4"
    delete_target = source_dir / "DELETE.mp4"
    rename_target.write_bytes(b"a")
    delete_target.write_bytes(b"b")
    job = ImportJob.create(
        source_path=str(source_dir),
        library=library,
        state="failed",
        failed_count=2,
        failed_files=json.dumps(
            [
                {"path": str(rename_target), "reason": "movie_number_not_found", "detail": ""},
                {"path": str(delete_target), "reason": "movie_number_not_found", "detail": ""},
            ],
            ensure_ascii=False,
        ),
    )
    headers = {"Authorization": f"Bearer {token}"}

    rename_response = client.post(
        f"/import-jobs/{job.id}/failed-files/rename",
        json={"path": str(rename_target), "new_name": "ABP-777.mp4"},
        headers=headers,
    )
    assert rename_response.status_code == 200
    assert (source_dir / "ABP-777.mp4").exists()
    assert not rename_target.exists()

    delete_response = client.request(
        "DELETE",
        f"/import-jobs/{job.id}/failed-files",
        json={"path": str(delete_target)},
        headers=headers,
    )
    assert delete_response.status_code == 200
    assert not delete_target.exists()
    remaining = {item["path"] for item in delete_response.json()["failed_files"]}
    assert str(delete_target) not in remaining
    assert str(source_dir / "ABP-777.mp4") in remaining
