import pytest

from src.common import build_signed_clip_collection_playlist_url
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


def test_collection_resource_carries_playlist_url(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path, "ABC-100")
    clip = _make_clip(client, token, media, 0, 20)
    collection_id = client.post(
        "/clip-collections", json={"name": "p"}, headers=_auth(token)
    ).json()["id"]

    # 空合集不下发 playlist_url，避免前端拉到 404 m3u8。
    empty_detail = client.get(
        f"/clip-collections/{collection_id}", headers=_auth(token)
    ).json()
    assert empty_detail["playlist_url"] is None

    client.put(
        f"/clip-collections/{collection_id}/clips/{clip}", headers=_auth(token)
    )
    detail = client.get(
        f"/clip-collections/{collection_id}", headers=_auth(token)
    ).json()
    assert detail["playlist_url"] is not None
    assert detail["playlist_url"].startswith(
        f"/clip-collections/{collection_id}/playlist.m3u8?"
    )


def test_playlist_m3u8_signature_required(client, account_user):
    token = _login(client, username=account_user.username)
    collection_id = client.post(
        "/clip-collections", json={"name": "sign"}, headers=_auth(token)
    ).json()["id"]

    # m3u8 端点本身不依赖账号 Cookie，仅靠签名 URL 校验；缺签名直接 403。
    no_sig = client.get(f"/clip-collections/{collection_id}/playlist.m3u8")
    assert no_sig.status_code == 403

    bad_sig = client.get(
        f"/clip-collections/{collection_id}/playlist.m3u8",
        params={"expires": 1700000000 + 3600, "signature": "deadbeef"},
    )
    assert bad_sig.status_code == 403


def test_playlist_m3u8_empty_collection_returns_404(client, account_user):
    token = _login(client, username=account_user.username)
    collection_id = client.post(
        "/clip-collections", json={"name": "empty"}, headers=_auth(token)
    ).json()["id"]

    signed_url = build_signed_clip_collection_playlist_url(collection_id)
    response = client.get(signed_url)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "clip_collection_empty"


def test_playlist_m3u8_contains_signed_clip_segments(
    client, account_user, tmp_path, clip_storage
):
    token = _login(client, username=account_user.username)
    media_a = _create_media(tmp_path, "ABC-201")
    media_b = _create_media(tmp_path, "ABC-202")
    clip_a = _make_clip(client, token, media_a, 0, 10)
    clip_b = _make_clip(client, token, media_b, 10, 30)
    collection_id = client.post(
        "/clip-collections", json={"name": "play"}, headers=_auth(token)
    ).json()["id"]
    client.put(f"/clip-collections/{collection_id}/clips/{clip_a}", headers=_auth(token))
    client.put(f"/clip-collections/{collection_id}/clips/{clip_b}", headers=_auth(token))

    signed_url = build_signed_clip_collection_playlist_url(collection_id)
    response = client.get(signed_url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    body = response.text

    # 头部约束：VOD + ENDLIST + DISCONTINUITY；两个片段两段 EXTINF。
    assert body.startswith("#EXTM3U\n")
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in body
    assert "#EXT-X-ENDLIST" in body
    assert body.count("#EXTINF:") == 2
    assert body.count("#EXT-X-DISCONTINUITY") == 1
    # 分片 URL 复用 build_signed_clip_url，路径与签名串一致。
    assert f"/media-clips/{clip_a}/stream?expires=" in body
    assert f"/media-clips/{clip_b}/stream?expires=" in body


def test_collection_thumbnails_offset_accumulates_across_clips(
    client, account_user, tmp_path, clip_storage
):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path, "ABC-300")
    # 同源切两个区间：[0,10] 与 [10,30]，缩略图在源轴上分别覆盖 {0,10} 与 {10,20,30}。
    clip_a = _make_clip(client, token, media, 0, 10)
    clip_b = _make_clip(client, token, media, 10, 30)
    collection_id = client.post(
        "/clip-collections", json={"name": "thumb"}, headers=_auth(token)
    ).json()["id"]
    client.put(f"/clip-collections/{collection_id}/clips/{clip_a}", headers=_auth(token))
    client.put(f"/clip-collections/{collection_id}/clips/{clip_b}", headers=_auth(token))

    listed = client.get(
        f"/clip-collections/{collection_id}/thumbnails", headers=_auth(token)
    )
    assert listed.status_code == 200
    payload = listed.json()
    offsets = [(item["clip_id"], item["offset_seconds"]) for item in payload]

    # clip_a 区间 [0,10] 内缩略图：源 0,10 → 片段相对 0,10。
    # clip_b 区间 [10,30] 内缩略图：源 10,20,30 → 片段相对 0,10,20；
    # 叠加前序 clip_a duration=10 后合集时间轴为 10,20,30。
    assert offsets == [
        (clip_a, 0),
        (clip_a, 10),
        (clip_b, 10),
        (clip_b, 20),
        (clip_b, 30),
    ]
    assert all(item["image"]["origin"] for item in payload)
    assert all("width" in item and "height" in item for item in payload)
