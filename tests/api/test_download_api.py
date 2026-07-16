from src.api.exception.errors import ApiError
from src.model import DownloadClient, MediaLibrary
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import (
    DownloadClientStorageDirectoryMappingResult,
    DownloadClientStorageHardlinkResult,
    DownloadClientStorageTestResponse,
    DownloadClientTestResponse,
    DownloadRequestCreateResponse,
    DownloadTaskActionResponse,
    DownloadTaskResource,
)


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def test_download_endpoints_require_authentication(client):
    assert client.get("/download-clients").status_code == 401
    assert client.post("/download-clients", json={}).status_code == 401
    assert client.patch("/download-clients/1", json={}).status_code == 401
    assert client.delete("/download-clients/1").status_code == 401
    assert client.get("/download-clients/1/test").status_code == 401
    assert client.post("/download-clients/1/storage-test").status_code == 401
    assert client.post("/download-clients/probe/test", json={}).status_code == 401
    assert client.post("/download-clients/probe/storage-test", json={}).status_code == 401
    assert client.get("/download-candidates", params={"movie_number": "ABC-001"}).status_code == 401
    assert client.post("/download-requests", json={}).status_code == 401
    assert client.get("/download-tasks").status_code == 401
    assert client.get("/download-tasks/stream").status_code == 401
    assert client.post("/download-tasks/1/pause").status_code == 401
    assert client.post("/download-tasks/1/resume").status_code == 401
    assert client.delete("/download-tasks/1").status_code == 401


def test_download_client_crud_api(client, account_user):
    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": "/library/main"})

    create_response = client.post(
        "/download-clients",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": " client-a ",
            "base_url": " http://localhost:8080 ",
            "username": " alice ",
            "password": " secret ",
            "client_save_path": " /downloads/a ",
            "local_root_path": " /mnt/downloads/a ",
            "media_library_id": library.id,
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["client_save_path"] == "/downloads/a"
    assert created["local_root_path"] == "/mnt/downloads/a"

    list_response = client.get(
        "/download-clients",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    update_response = client.patch(
        f"/download-clients/{created['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "client-renamed"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "client-renamed"

    stored = DownloadClient.get_by_id(created["id"])
    assert stored.password == "secret"

    delete_response = client.delete(
        f"/download-clients/{created['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert delete_response.status_code == 204
    assert DownloadClient.get_or_none(DownloadClient.id == created["id"]) is None


def test_download_client_rejects_cloud115_library(client, account_user):
    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(
        name="Cloud",
        backend="cloud115",
        backend_account_key="cloud115:download-client-mismatch",
        backend_config={"cookies": "UID=x_A1_y", "root_cid": "root", "app": "web"},
    )

    response = client.post(
        "/download-clients",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "client-cloud",
            "base_url": "http://localhost:8080",
            "username": "alice",
            "password": "secret",
            "client_save_path": "/downloads/a",
            "local_root_path": "/mnt/downloads/a",
            "media_library_id": library.id,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_library_backend_mismatch"
    assert DownloadClient.select().count() == 0


def test_cloud115_download_client_api_enforces_one_per_library_and_immutable_binding(
    client, account_user
):
    token = _login(client, username=account_user.username)
    first_library = MediaLibrary.create(
        name="Cloud A",
        backend="cloud115",
        backend_account_key="cloud115:client-a",
        backend_config={"cookies": "UID=a_A1_1", "root_cid": "root-a"},
    )
    second_library = MediaLibrary.create(
        name="Cloud B",
        backend="cloud115",
        backend_account_key="cloud115:client-b",
        backend_config={"cookies": "UID=b_A1_1", "root_cid": "root-b"},
    )
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/download-clients",
        headers=headers,
        json={
            "name": "cloud115-a",
            "kind": "cloud115",
            "media_library_id": first_library.id,
        },
    )
    assert created.status_code == 201

    duplicate = client.post(
        "/download-clients",
        headers=headers,
        json={
            "name": "cloud115-b",
            "kind": "cloud115",
            "media_library_id": first_library.id,
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "cloud115_download_client_already_exists"

    rebound = client.patch(
        f"/download-clients/{created.json()['id']}",
        headers=headers,
        json={"media_library_id": second_library.id},
    )
    assert rebound.status_code == 409
    assert rebound.json()["error"]["code"] == "cloud115_download_client_library_immutable"


def test_download_client_api_reports_expected_errors(client, account_user):
    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": "/library/main"})
    DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path="/mnt/downloads/a",
        media_library=library,
    )

    conflict = client.post(
        "/download-clients",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "client-a",
            "base_url": "http://localhost:8081",
            "username": "bob",
            "password": "secret",
            "client_save_path": "/downloads/b",
            "local_root_path": "/mnt/downloads/b",
            "media_library_id": library.id,
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "download_client_name_conflict"

    invalid_url = client.post(
        "/download-clients",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "client-b",
            "base_url": "localhost:8080",
            "username": "bob",
            "password": "secret",
            "client_save_path": "/downloads/b",
            "local_root_path": "/mnt/downloads/b",
            "media_library_id": library.id,
        },
    )
    assert invalid_url.status_code == 422
    assert invalid_url.json()["error"]["code"] == "invalid_download_client_base_url"


def test_download_client_test_api_returns_qb_status(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadClientService.test_client",
        staticmethod(
            lambda client_id: DownloadClientTestResponse(
                healthy=True,
                checked_at="2026-03-10T08:00:00",
                client_id=client_id,
                client_name="client-a",
                base_url="http://localhost:8080",
                elapsed_ms=12,
                version="5.0.4",
                web_api_version="2.11.4",
            )
        ),
    )

    response = client.get(
        "/download-clients/3/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["healthy"] is True
    assert data["client_id"] == 3
    assert data["version"] == "5.0.4"
    assert data["web_api_version"] == "2.11.4"
    assert data["error"] is None


def test_download_client_test_api_returns_unhealthy_result(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadClientService.test_client",
        staticmethod(
            lambda client_id: {
                "healthy": False,
                "checked_at": "2026-03-10T08:00:00",
                "client_id": client_id,
                "client_name": "client-a",
                "base_url": "http://localhost:8080",
                "elapsed_ms": 15,
                "version": None,
                "web_api_version": None,
                "error": {
                    "type": "qbittorrent_request_error",
                    "message": "login failed",
                },
            }
        ),
    )

    response = client.get(
        "/download-clients/3/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["healthy"] is False
    assert data["error"]["type"] == "qbittorrent_request_error"
    assert data["error"]["message"] == "login failed"


def test_download_client_storage_test_api_returns_result(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadClientService.test_storage",
        staticmethod(
            lambda client_id: DownloadClientStorageTestResponse(
                healthy=True,
                checked_at="2026-03-10T08:00:00",
                client_id=client_id,
                client_name="client-a",
                elapsed_ms=20,
                warnings=[],
                directory_mapping=DownloadClientStorageDirectoryMappingResult(
                    status="ok",
                    client_save_path="/downloads/a",
                    local_root_path="/mnt/downloads/a",
                    probe_remote_dir="/downloads/a/.sakuramedia-diagnostics/probe",
                    probe_local_dir="/mnt/downloads/a/.sakuramedia-diagnostics/probe",
                    sentinel_visible_to_qb=True,
                ),
                hardlink=DownloadClientStorageHardlinkResult(
                    status="ok",
                    supported=True,
                    source_path="/mnt/downloads/a/.sakuramedia-diagnostics/probe/sentinel.txt",
                    target_path="/library/.sakuramedia-diagnostics/probe/sentinel.link",
                ),
            )
        ),
    )

    response = client.post(
        "/download-clients/3/storage-test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["healthy"] is True
    assert data["client_id"] == 3
    assert data["directory_mapping"]["status"] == "ok"
    assert data["hardlink"]["supported"] is True


def test_download_client_storage_test_api_returns_warning_result(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadClientService.test_storage",
        staticmethod(
            lambda client_id: {
                "healthy": True,
                "checked_at": "2026-03-10T08:00:00",
                "client_id": client_id,
                "client_name": "client-a",
                "elapsed_ms": 22,
                "warnings": ["下载目录到媒体库不支持硬链接，导入会回退为复制"],
                "directory_mapping": {
                    "status": "ok",
                    "client_save_path": "/downloads/a",
                    "local_root_path": "/mnt/downloads/a",
                    "probe_remote_dir": "/downloads/a/.sakuramedia-diagnostics/probe",
                    "probe_local_dir": "/mnt/downloads/a/.sakuramedia-diagnostics/probe",
                    "sentinel_visible_to_qb": True,
                    "error": None,
                },
                "hardlink": {
                    "status": "failed",
                    "supported": False,
                    "source_path": "/mnt/downloads/a/.sakuramedia-diagnostics/probe/sentinel.txt",
                    "target_path": "/library/.sakuramedia-diagnostics/probe/sentinel.link",
                    "error": {
                        "type": "hardlink_not_supported",
                        "message": "Invalid cross-device link",
                    },
                },
            }
        ),
    )

    response = client.post(
        "/download-clients/3/storage-test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["healthy"] is True
    assert data["hardlink"]["status"] == "failed"
    assert data["warnings"] == ["下载目录到媒体库不支持硬链接，导入会回退为复制"]


def test_download_client_probe_test_api_forwards_payload(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    captured = {}

    def fake_probe_test(payload):
        captured["payload"] = payload
        return DownloadClientTestResponse(
            healthy=True,
            checked_at="2026-03-10T08:00:00",
            client_id=0,
            client_name="",
            base_url=payload.base_url,
            elapsed_ms=8,
            version="5.0.4",
            web_api_version="2.11.4",
        )

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadClientService.probe_test",
        staticmethod(fake_probe_test),
    )

    response = client.post(
        "/download-clients/probe/test",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "base_url": "http://qb.example.com",
            "username": "alice",
            "password": "fresh",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["healthy"] is True
    assert data["client_id"] == 0
    assert data["client_name"] == ""
    assert data["base_url"] == "http://qb.example.com"
    assert captured["payload"].password == "fresh"
    assert captured["payload"].client_id is None


def test_download_client_probe_storage_test_api_forwards_payload(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    captured = {}

    def fake_probe_storage(payload):
        captured["payload"] = payload
        return DownloadClientStorageTestResponse(
            healthy=True,
            checked_at="2026-03-10T08:00:00",
            client_id=payload.client_id or 0,
            client_name="",
            elapsed_ms=15,
            warnings=[],
            directory_mapping=DownloadClientStorageDirectoryMappingResult(
                status="ok",
                client_save_path=payload.client_save_path,
                local_root_path=payload.local_root_path,
                probe_remote_dir="/downloads/a/.sakuramedia-diagnostics/probe",
                probe_local_dir="/mnt/downloads/a/.sakuramedia-diagnostics/probe",
                sentinel_visible_to_qb=True,
            ),
            hardlink=DownloadClientStorageHardlinkResult(
                status="ok",
                supported=True,
                source_path="/mnt/downloads/a/.sakuramedia-diagnostics/probe/sentinel.txt",
                target_path="/library/.sakuramedia-diagnostics/probe/sentinel.link",
            ),
        )

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadClientService.probe_storage_test",
        staticmethod(fake_probe_storage),
    )

    response = client.post(
        "/download-clients/probe/storage-test",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "base_url": "http://qb.example.com",
            "username": "alice",
            "password": None,
            "client_save_path": "/downloads/a",
            "local_root_path": "/mnt/downloads/a",
            "media_library_id": 7,
            "client_id": 3,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["healthy"] is True
    assert data["directory_mapping"]["client_save_path"] == "/downloads/a"
    assert captured["payload"].client_id == 3
    assert captured["payload"].password is None


def test_download_candidates_api_uses_search_service(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadSearchService.search_candidates",
        lambda self, movie_number, indexer_kind=None: [
            {
                "source": "jackett",
                "indexer_name": "mteam",
                "indexer_kind": "pt",
                "resolved_client_id": 1,
                "resolved_client_name": "client-a",
                "resolved_client_kind": "qbittorrent",
                "download_clients": [
                    {"id": 1, "name": "client-a", "kind": "qbittorrent"}
                ],
                "movie_number": movie_number,
                "title": "ABC-001 4K",
                "size_bytes": 123,
                "seeders": 5,
                "magnet_url": "",
                "torrent_url": "https://example.com/1",
                "tags": ["4K"],
            }
        ],
    )

    response = client.get(
        "/download-candidates",
        headers={"Authorization": f"Bearer {token}"},
        params={"movie_number": "ABC-001", "indexer_kind": "pt"},
    )

    assert response.status_code == 200
    assert response.json()[0]["movie_number"] == "ABC-001"
    assert response.json()[0]["download_clients"] == [
        {"id": 1, "name": "client-a", "kind": "qbittorrent"}
    ]


def test_download_request_api_returns_201_or_200(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    created_response = DownloadRequestCreateResponse(
        task=DownloadTaskResource(
            id=1,
            client_id=1,
            movie_number="ABC-001",
            name="ABC-001",
            info_hash="hash-1",
            save_path="/mnt/downloads/a/ABC-001",
            progress=0.0,
            download_state="queued",
            import_status="pending",
            created_at="2026-03-10T08:10:00Z",
            updated_at="2026-03-10T08:10:00Z",
        ),
        created=True,
    )
    duplicate_response = created_response.model_copy(update={"created": False})
    responses = [created_response, duplicate_response]

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadRequestService.create_request",
        lambda self, payload: responses.pop(0),
    )

    payload = {
        "client_id": 1,
        "movie_number": "ABC-001",
        "candidate": {
            "source": "jackett",
            "indexer_name": "mteam",
            "indexer_kind": "pt",
            "title": "ABC-001",
            "size_bytes": 123,
            "seeders": 5,
            "magnet_url": "magnet:?xt=urn:btih:ABCDEF123456",
            "torrent_url": "",
            "tags": ["4K"],
        },
    }
    first = client.post("/download-requests", headers={"Authorization": f"Bearer {token}"}, json=payload)
    second = client.post("/download-requests", headers={"Authorization": f"Bearer {token}"}, json=payload)

    assert first.status_code == 201
    assert second.status_code == 200

def test_download_request_api_propagates_domain_errors(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadRequestService.create_request",
        lambda self, payload: (_ for _ in ()).throw(
            ApiError(422, "invalid_download_request_candidate", "bad request")
        ),
    )

    payload = {
        "client_id": 1,
        "movie_number": "ABC-001",
        "candidate": {
            "source": "jackett",
            "indexer_name": "mteam",
            "indexer_kind": "pt",
            "title": "ABC-001",
            "size_bytes": 123,
            "seeders": 5,
            "magnet_url": "",
            "torrent_url": "",
            "tags": [],
        },
    }
    response = client.post(
        "/download-requests",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_download_request_candidate"


def test_download_tasks_api_lists_controls_and_confirms_file_deletion(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    task = DownloadTaskResource(
        id=7,
        client_id=3,
        movie_number="ABC-001",
        name="ABC-001",
        info_hash="hash-1",
        save_path="/mnt/downloads/a/ABC-001",
        progress=0.5,
        download_state="downloading",
        import_status="pending",
        created_at="2026-03-10T08:10:00Z",
        updated_at="2026-03-10T08:10:00Z",
    )
    captured = {}

    def fake_list_tasks(**kwargs):
        captured["list"] = kwargs
        return PageResponse(items=[task], page=1, page_size=20, total=1)

    def fake_delete_task(task_id, delete_files):
        captured["delete"] = {"task_id": task_id, "delete_files": delete_files}
        return {"task_id": task_id, "client_id": 3, "movie_number": "ABC-001", "info_hash": "hash-1"}

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadTaskService.list_tasks",
        fake_list_tasks,
    )
    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadTaskService.pause_task",
        lambda task_id: DownloadTaskActionResponse(task_id=task_id, action="pause"),
    )
    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadTaskService.resume_task",
        lambda task_id: DownloadTaskActionResponse(task_id=task_id, action="resume"),
    )
    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadTaskService.delete_task",
        fake_delete_task,
    )

    listed = client.get(
        "/download-tasks",
        headers={"Authorization": f"Bearer {token}"},
        params={"client_id": 3, "movie_number": "ABC-001"},
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == 7
    assert captured["list"]["client_id"] == 3

    paused = client.post("/download-tasks/7/pause", headers={"Authorization": f"Bearer {token}"})
    resumed = client.post("/download-tasks/7/resume", headers={"Authorization": f"Bearer {token}"})
    assert paused.status_code == 200 and paused.json()["action"] == "pause"
    assert resumed.status_code == 200 and resumed.json()["action"] == "resume"

    rejected = client.delete(
        "/download-tasks/7",
        headers={"Authorization": f"Bearer {token}"},
        params={"delete_files": "true"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "download_task_delete_confirmation_required"

    deleted = client.delete(
        "/download-tasks/7",
        headers={"Authorization": f"Bearer {token}"},
        params={"delete_files": "true", "confirm_delete_files": "true"},
    )
    assert deleted.status_code == 204
    assert captured["delete"] == {"task_id": 7, "delete_files": True}


def test_download_task_stream_api_emits_sse_payload(client, account_user):
    token = _login(client, username=account_user.username)

    class FakeHub:
        def __init__(self):
            self.filters = None

        def subscribe(self, *, client_id=None, movie_number=None):
            self.filters = {"client_id": client_id, "movie_number": movie_number}
            return object()

        @staticmethod
        def iter_events(_subscription):
            yield "snapshot", {"client_id": 3, "items": []}

        @staticmethod
        def close():
            return None

    fake_hub = FakeHub()
    client.app.state.download_progress_hub = fake_hub
    response = client.get(
        "/download-tasks/stream",
        headers={"Authorization": f"Bearer {token}"},
        params={"client_id": 3, "movie_number": "ABC-001"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: snapshot" in response.text
    assert '"client_id": 3' in response.text
    assert fake_hub.filters == {"client_id": 3, "movie_number": "ABC-001"}
