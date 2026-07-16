from src.model import Media, MediaLibrary, MediaRapidUploadBatch, VideoItem
from src.service.transfers.import_runner import MediaRapidUploadRunner


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def test_media_rapid_upload_endpoints_require_authentication(client):
    assert client.post(
        "/media/rapid-uploads",
        json={"media_ids": [1], "target_library_id": 2},
    ).status_code == 401
    assert client.get("/media/rapid-uploads").status_code == 401
    assert client.get("/media/rapid-uploads/1").status_code == 401
    assert client.post("/media/rapid-uploads/1/retry").status_code == 401


def test_create_and_query_media_rapid_upload_batch(
    client,
    account_user,
    tmp_path,
    monkeypatch,
):
    token = _login(client, username=account_user.username)
    headers = {"Authorization": f"Bearer {token}"}
    local = MediaLibrary.create(
        name="local-api",
        backend="local",
        backend_config={"root_path": str(tmp_path)},
    )
    cloud = MediaLibrary.create(
        name="cloud-api",
        backend="cloud115",
        backend_config={
            "cookies": "UID=1_R2_1; CID=x; SEID=y",
            "root_cid": "root-cid",
            "app": "alipaymini",
        },
    )
    source = tmp_path / "api.mp4"
    source.write_bytes(b"video")
    video = VideoItem.create(title="api-video")
    media = Media.create(
        video_item=video,
        library=local,
        path=str(source),
        file_size_bytes=source.stat().st_size,
    )
    monkeypatch.setattr(MediaRapidUploadRunner, "submit", lambda *args, **kwargs: None)

    response = client.post(
        "/media/rapid-uploads",
        json={"media_ids": [media.id], "target_library_id": cloud.id},
        headers=headers,
    )

    assert response.status_code == 202
    batch_id = response.json()["rapid_upload_batch_id"]
    assert MediaRapidUploadBatch.get_by_id(batch_id).total_count == 1

    list_response = client.get("/media/rapid-uploads", headers=headers)
    detail_response = client.get(f"/media/rapid-uploads/{batch_id}", headers=headers)
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert detail_response.status_code == 200
    assert detail_response.json()["items"][0]["media_id"] == media.id
