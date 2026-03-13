from src.api.exception.errors import ApiError
from src.model import DownloadClient, DownloadTask, MediaLibrary
from src.schema.transfers.downloads import (
    DownloadClientSyncResponse,
    DownloadRequestCreateResponse,
    DownloadTaskImportResponse,
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
    assert client.get("/download-candidates", params={"movie_number": "ABC-001"}).status_code == 401
    assert client.post("/download-requests", json={}).status_code == 401
    assert client.post("/download-clients/1/sync").status_code == 401
    assert client.get("/download-tasks").status_code == 401
    assert client.post("/download-tasks/1/import").status_code == 401
    assert client.delete("/download-tasks").status_code == 401


def test_download_client_crud_api(client, account_user):
    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(name="Main", root_path="/library/main")

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


def test_download_client_api_reports_expected_errors(client, account_user):
    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(name="Main", root_path="/library/main")
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


def test_download_candidates_api_uses_search_service(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadSearchService.search_candidates",
        lambda self, movie_number, indexer_kind=None: [
            {
                "source": "jackett",
                "indexer_name": "mteam",
                "indexer_kind": "pt",
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


def test_download_sync_api_returns_summary(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadSyncService.sync_client",
        lambda self, client_id: DownloadClientSyncResponse(
            client_id=client_id,
            scanned_count=2,
            created_count=1,
            updated_count=1,
            unchanged_count=0,
        ),
    )

    response = client.post(
        "/download-clients/7/sync",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["client_id"] == 7


def test_download_task_import_api_returns_202(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    monkeypatch.setattr(
        "src.api.routers.transfers.downloads.DownloadTaskService.trigger_import",
        lambda task_id: DownloadTaskImportResponse(task_id=task_id, import_job_id=5, status="accepted"),
    )

    response = client.post(
        "/download-tasks/9/import",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    assert response.json() == {"task_id": 9, "import_job_id": 5, "status": "accepted"}


def test_download_task_list_and_delete_api(client, account_user):
    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(name="Main", root_path="/library/main")
    client_row = DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path="/mnt/downloads/a",
        media_library=library,
    )
    first = DownloadTask.create(
        client=client_row,
        movie="ABC-001",
        name="task-1",
        info_hash="hash-1",
        save_path="/mnt/downloads/a/task-1",
        progress=0.2,
        download_state="downloading",
        import_status="pending",
    )
    second = DownloadTask.create(
        client=client_row,
        movie="ABC-001",
        name="task-2",
        info_hash="hash-2",
        save_path="/mnt/downloads/a/task-2",
        progress=1.0,
        download_state="completed",
        import_status="completed",
    )

    response = client.get(
        "/download-tasks",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "page": 1,
            "page_size": 20,
            "download_state": "completed",
            "import_status": "completed",
            "movie_number": "abc-001",
            "query": "hash-2",
            "sort": "created_at:asc",
        },
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == second.id

    delete_response = client.delete(
        "/download-tasks",
        headers={"Authorization": f"Bearer {token}"},
        params={"task_ids": str(first.id)},
    )
    assert delete_response.status_code == 204
    assert DownloadTask.get_or_none(DownloadTask.id == first.id) is None


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
