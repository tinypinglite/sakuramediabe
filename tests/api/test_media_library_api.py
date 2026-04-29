from src.model import DownloadClient, ImportJob, Media, MediaLibrary, Movie


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def test_media_library_endpoints_require_authentication(client):
    create_response = client.post("/media-libraries", json={"name": "Main", "root_path": "/library/main"})
    list_response = client.get("/media-libraries")
    update_response = client.patch("/media-libraries/1", json={"name": "Updated"})
    delete_response = client.delete("/media-libraries/1")

    assert create_response.status_code == 401
    assert list_response.status_code == 401
    assert update_response.status_code == 401
    assert delete_response.status_code == 401


def test_list_media_libraries_returns_sorted_resources(client, account_user):
    token = _login(client, username=account_user.username)
    first = MediaLibrary.create(name="A", root_path="/library/a")
    second = MediaLibrary.create(name="B", root_path="/library/b")

    response = client.get(
        "/media-libraries",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [second.id, first.id]


def test_create_media_library(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/media-libraries",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": " Main ", "root_path": " /library/main "},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Main"
    assert response.json()["root_path"] == "/library/main"
    assert MediaLibrary.get_by_id(response.json()["id"]).name == "Main"


def test_create_media_library_reports_conflicts_and_validation_errors(client, account_user):
    token = _login(client, username=account_user.username)
    MediaLibrary.create(name="Main", root_path="/library/main")

    name_conflict = client.post(
        "/media-libraries",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Main", "root_path": "/library/other"},
    )
    path_conflict = client.post(
        "/media-libraries",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Other", "root_path": "/library/main"},
    )
    invalid_path = client.post(
        "/media-libraries",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Other", "root_path": "relative/path"},
    )

    assert name_conflict.status_code == 409
    assert name_conflict.json()["error"]["code"] == "media_library_name_conflict"
    assert path_conflict.status_code == 409
    assert path_conflict.json()["error"]["code"] == "media_library_root_path_conflict"
    assert invalid_path.status_code == 422
    assert invalid_path.json()["error"]["code"] == "invalid_media_library_root_path"


def test_update_media_library_supports_name_and_root_path_changes(client, account_user):
    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(name="Main", root_path="/library/main")

    response = client.patch(
        f"/media-libraries/{library.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Archive", "root_path": "/library/archive"},
    )

    library = MediaLibrary.get_by_id(library.id)
    assert response.status_code == 200
    assert response.json()["name"] == "Archive"
    assert response.json()["root_path"] == "/library/archive"
    assert library.name == "Archive"
    assert library.root_path == "/library/archive"


def test_update_media_library_reports_expected_errors(client, account_user):
    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(name="Main", root_path="/library/main")
    MediaLibrary.create(name="Other", root_path="/library/other")

    empty_payload = client.patch(
        f"/media-libraries/{library.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    missing = client.patch(
        "/media-libraries/999",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Updated"},
    )
    name_conflict = client.patch(
        f"/media-libraries/{library.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Other"},
    )
    path_conflict = client.patch(
        f"/media-libraries/{library.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"root_path": "/library/other"},
    )
    invalid_path = client.patch(
        f"/media-libraries/{library.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"root_path": "relative/path"},
    )

    assert empty_payload.status_code == 422
    assert empty_payload.json()["error"]["code"] == "empty_media_library_update"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "media_library_not_found"
    assert name_conflict.status_code == 409
    assert name_conflict.json()["error"]["code"] == "media_library_name_conflict"
    assert path_conflict.status_code == 409
    assert path_conflict.json()["error"]["code"] == "media_library_root_path_conflict"
    assert invalid_path.status_code == 422
    assert invalid_path.json()["error"]["code"] == "invalid_media_library_root_path"


def test_delete_media_library_succeeds_without_references(client, account_user):
    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(name="Main", root_path="/library/main")

    response = client.delete(
        f"/media-libraries/{library.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert MediaLibrary.get_or_none(MediaLibrary.id == library.id) is None


def test_delete_media_library_returns_conflict_for_references(client, account_user):
    token = _login(client, username=account_user.username)

    media_library = MediaLibrary.create(name="Media", root_path="/library/media")
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")
    Media.create(movie=movie, path="/library/media/video.mp4", library=media_library)

    download_library = MediaLibrary.create(name="Downloads", root_path="/library/downloads")
    DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path="/mnt/downloads/a",
        media_library=download_library,
    )

    import_library = MediaLibrary.create(name="Import", root_path="/library/import")
    ImportJob.create(source_path="/downloads/import", library=import_library)

    media_response = client.delete(
        f"/media-libraries/{media_library.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    download_response = client.delete(
        f"/media-libraries/{download_library.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    import_response = client.delete(
        f"/media-libraries/{import_library.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert media_response.status_code == 409
    assert media_response.json()["error"]["code"] == "media_library_in_use"
    assert download_response.status_code == 409
    assert download_response.json()["error"]["code"] == "media_library_in_use"
    assert import_response.status_code == 409
    assert import_response.json()["error"]["code"] == "media_library_in_use"
