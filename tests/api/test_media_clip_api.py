import hashlib
import hmac
from pathlib import Path

import pytest

from src.config.config import settings
from src.model import Image, Media, MediaClip, MediaThumbnail, Movie
from src.service.playback.media_clip_service import MediaClipService
from tests.conftest import TEST_FILE_SIGNATURE_EXPIRES, TEST_FILE_SIGNATURE_SECRET


def _login(client, username="account", password="password123"):
    response = client.post("/auth/tokens", json={"username": username, "password": password})
    return response.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_media(tmp_path, movie_number="ABC-001") -> Media:
    movie = Movie.create(movie_number=movie_number, javdb_id=f"jav-{movie_number}", title=movie_number)
    source = tmp_path / f"{movie_number}.mp4"
    source.write_bytes(b"video-bytes")
    media = Media.create(movie=movie, path=str(source), valid=True)
    for offset in (0, 10, 20, 30):
        path = f"movies/{movie_number}/media/fp/thumbnails/{offset}.webp"
        image = Image.create(origin=path, small=path, medium=path, large=path)
        MediaThumbnail.create(media=media, image=image, offset=offset)
    return media


def _thumb_id(media, offset):
    return MediaThumbnail.get(MediaThumbnail.media == media, MediaThumbnail.offset == offset).id


@pytest.fixture()
def clip_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path / "clips"))
    monkeypatch.setattr(
        MediaClipService,
        "_cut_clip_file",
        staticmethod(lambda source, target, start, end: target.write_bytes(b"clip-bytes")),
    )


def _build_signed_clip_url(clip_id, expires=TEST_FILE_SIGNATURE_EXPIRES):
    payload = f"clip:{clip_id}:{expires}"
    signature = hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"/media-clips/{clip_id}/stream?expires={expires}&signature={signature}"


def test_clip_endpoints_require_authentication(client):
    assert client.post("/media/1/clips", json={"start_thumbnail_id": 1, "end_thumbnail_id": 2}).status_code == 401
    assert client.get("/media/1/clips").status_code == 401
    assert client.get("/media-clips").status_code == 401
    assert client.get("/media-clips/1").status_code == 401
    assert client.patch("/media-clips/1", json={"title": "x"}).status_code == 401
    assert client.delete("/media-clips/1").status_code == 401


def test_create_clip_returns_201_then_200_on_duplicate(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    body = {"start_thumbnail_id": _thumb_id(media, 10), "end_thumbnail_id": _thumb_id(media, 30)}

    first = client.post(f"/media/{media.id}/clips", json=body, headers=_auth(token))
    second = client.post(f"/media/{media.id}/clips", json=body, headers=_auth(token))

    assert first.status_code == 201
    payload = first.json()
    assert payload["start_offset_seconds"] == 10
    assert payload["end_offset_seconds"] == 30
    assert payload["cover_image"] is not None
    assert payload["stream_url"].startswith("/media-clips/")
    assert second.status_code == 200
    assert second.json()["clip_id"] == payload["clip_id"]


def test_create_clip_rejects_invalid_range(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    body = {"start_thumbnail_id": _thumb_id(media, 10), "end_thumbnail_id": _thumb_id(media, 10)}

    response = client.post(f"/media/{media.id}/clips", json=body, headers=_auth(token))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_clip_invalid_range"


def test_list_and_detail_clip(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    created = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 20)},
        headers=_auth(token),
    ).json()

    per_media = client.get(f"/media/{media.id}/clips", headers=_auth(token))
    global_list = client.get("/media-clips", headers=_auth(token))
    detail = client.get(f"/media-clips/{created['clip_id']}", headers=_auth(token))

    assert per_media.status_code == 200
    assert len(per_media.json()) == 1
    assert global_list.status_code == 200
    assert global_list.json()["total"] == 1
    assert detail.status_code == 200
    # 区间 [0, 20] 内缩略图为 0/10/20 三帧。
    assert len(detail.json()["preview_frames"]) == 3
    # 未加入任何合集时 collections 为空数组（前端选择器据此回显勾选）。
    assert detail.json()["collections"] == []


def test_update_and_delete_clip(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    clip_id = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 10)},
        headers=_auth(token),
    ).json()["clip_id"]

    patched = client.patch(f"/media-clips/{clip_id}", json={"title": "片段A"}, headers=_auth(token))
    assert patched.status_code == 200
    assert patched.json()["title"] == "片段A"

    deleted = client.delete(f"/media-clips/{clip_id}", headers=_auth(token))
    assert deleted.status_code == 204
    assert MediaClip.get_or_none(MediaClip.id == clip_id) is None


def test_stream_clip_with_signature(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    clip_id = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 10)},
        headers=_auth(token),
    ).json()["clip_id"]

    full = client.get(_build_signed_clip_url(clip_id))
    partial = client.get(_build_signed_clip_url(clip_id), headers={"Range": "bytes=0-3"})

    assert full.status_code == 200
    assert full.content == b"clip-bytes"
    assert full.headers["accept-ranges"] == "bytes"
    assert partial.status_code == 206
    assert partial.content == b"clip"


def test_stream_clip_rejects_invalid_signature(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    clip_id = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 10)},
        headers=_auth(token),
    ).json()["clip_id"]

    response = client.get(f"/media-clips/{clip_id}/stream?expires=1&signature=bad")

    assert response.status_code == 403


def test_media_clips_path_does_not_collide_with_media_detail(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 10)},
        headers=_auth(token),
    )

    # /media-clips 不应被 /media/{media_id} 之类的动态路由吞掉。
    response = client.get("/media-clips", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["total"] == 1
