from src.api.exception.errors import ApiError
from src.model import DownloadClient, MediaLibrary
from src.schema.transfers.downloads import (
    DownloadRequestCreateResponse,
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
                "resolved_client_id": 1,
                "resolved_client_name": "client-a",
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
