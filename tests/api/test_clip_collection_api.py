import pytest

from src.config.config import settings
from src.model import Image, Media, MediaThumbnail, Movie
from src.service.playback.media_clip_service import MediaClipService


def _login(client, username="account", password="password123"):
    response = client.post("/auth/tokens", json={"username": username, "password": password})
    return response.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_media(tmp_path, movie_number) -> Media:
    movie = Movie.create(movie_number=movie_number, javdb_id=f"jav-{movie_number}", title=movie_number)
    source = tmp_path / f"{movie_number}.mp4"
    source.write_bytes(b"video-bytes")
    media = Media.create(movie=movie, path=str(source), valid=True)
    for offset in (0, 10, 20, 30):
        path = f"movies/{movie_number}/media/fp/thumbnails/{offset}.webp"
        image = Image.create(origin=path, small=path, medium=path, large=path)
        MediaThumbnail.create(media=media, image=image, offset=offset)
    return media


@pytest.fixture()
def clip_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path / "clips"))
    monkeypatch.setattr(
        MediaClipService,
        "_cut_clip_file",
        staticmethod(lambda source, target, start, end: target.write_bytes(b"clip-bytes")),
    )


def _make_clip(client, token, media, start, end):
    start_id = MediaThumbnail.get(MediaThumbnail.media == media, MediaThumbnail.offset == start).id
    end_id = MediaThumbnail.get(MediaThumbnail.media == media, MediaThumbnail.offset == end).id
    return client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": start_id, "end_thumbnail_id": end_id},
        headers=_auth(token),
    ).json()["clip_id"]


def test_clip_collection_endpoints_require_authentication(client):
    assert client.get("/clip-collections").status_code == 401
    assert client.post("/clip-collections", json={"name": "x"}).status_code == 401
    assert client.get("/clip-collections/1").status_code == 401
    assert client.get("/clip-collections/1/clips").status_code == 401
    assert client.put("/clip-collections/1/clips/1").status_code == 401


def test_create_collection_conflict(client, account_user):
    token = _login(client, username=account_user.username)

    created = client.post("/clip-collections", json={"name": "我的合集"}, headers=_auth(token))
    conflict = client.post("/clip-collections", json={"name": "我的合集"}, headers=_auth(token))

    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "clip_collection_name_conflict"


def test_collection_membership_and_reorder(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media_a = _create_media(tmp_path, "ABC-001")
    media_b = _create_media(tmp_path, "ABC-002")
    clip_a = _make_clip(client, token, media_a, 0, 10)
    clip_b = _make_clip(client, token, media_b, 10, 30)

    collection_id = client.post("/clip-collections", json={"name": "连播"}, headers=_auth(token)).json()["id"]
    assert client.put(f"/clip-collections/{collection_id}/clips/{clip_a}", headers=_auth(token)).status_code == 204
    assert client.put(f"/clip-collections/{collection_id}/clips/{clip_b}", headers=_auth(token)).status_code == 204

    listed = client.get(f"/clip-collections/{collection_id}/clips", headers=_auth(token)).json()
    assert [item["clip_id"] for item in listed["items"]] == [clip_a, clip_b]

    # 全量有序设置：翻转顺序。
    reorder = client.put(
        f"/clip-collections/{collection_id}/clips",
        json={"clip_ids": [clip_b, clip_a]},
        headers=_auth(token),
    )
    assert reorder.status_code == 204
    reordered = client.get(f"/clip-collections/{collection_id}/clips", headers=_auth(token)).json()
    assert [item["clip_id"] for item in reordered["items"]] == [clip_b, clip_a]

    removed = client.delete(f"/clip-collections/{collection_id}/clips/{clip_a}", headers=_auth(token))
    assert removed.status_code == 204
    assert client.get(f"/clip-collections/{collection_id}/clips", headers=_auth(token)).json()["total"] == 1


def test_collection_list_reports_count(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path, "ABC-003")
    clip = _make_clip(client, token, media, 0, 20)
    collection_id = client.post("/clip-collections", json={"name": "c"}, headers=_auth(token)).json()["id"]
    client.put(f"/clip-collections/{collection_id}/clips/{clip}", headers=_auth(token))

    listed = client.get("/clip-collections", headers=_auth(token)).json()
    target = next(item for item in listed if item["id"] == collection_id)
    assert target["clip_count"] == 1
    assert target["cover_image"] is not None


def test_delete_collection(client, account_user):
    token = _login(client, username=account_user.username)
    collection_id = client.post("/clip-collections", json={"name": "del"}, headers=_auth(token)).json()["id"]

    deleted = client.delete(f"/clip-collections/{collection_id}", headers=_auth(token))
    assert deleted.status_code == 204
    assert client.get(f"/clip-collections/{collection_id}", headers=_auth(token)).status_code == 404
