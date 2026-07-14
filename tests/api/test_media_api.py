from datetime import datetime

from src.model import (
    ClipCollection,
    ClipCollectionItem,
    Image,
    Media,
    MediaClip,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    PlaylistMovie,
    ResourceTaskState,
    VideoItem,
)
from src.service.collections import PlaylistService
from src.service.playback import MediaService


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def _create_thumbnail(media: Media, offset_seconds: int, suffix: str | None = None) -> MediaThumbnail:
    marker = suffix or str(offset_seconds)
    movie_number = media.movie.movie_number if hasattr(media.movie, "movie_number") else str(media.movie)
    image = Image.create(
        origin=f"movies/{movie_number}/media/fingerprint-{marker}/thumbnails/{offset_seconds}.webp",
        small=f"movies/{movie_number}/media/fingerprint-{marker}/thumbnails/{offset_seconds}.webp",
        medium=f"movies/{movie_number}/media/fingerprint-{marker}/thumbnails/{offset_seconds}.webp",
        large=f"movies/{movie_number}/media/fingerprint-{marker}/thumbnails/{offset_seconds}.webp",
    )
    return MediaThumbnail.create(media=media, image=image, offset=offset_seconds)


def _create_image(path: str) -> Image:
    return Image.create(origin=path, small=path, medium=path, large=path)


def test_update_media_progress_requires_authentication(client):
    response = client.put("/media/1/progress", json={"position_seconds": 600})
    thumbnails_response = client.get("/media/1/thumbnails")
    validity_check_response = client.post("/media/1/validity-check")
    media_points_response = client.get("/media-points")
    media_point_list_response = client.get("/media/1/points")
    media_point_create_response = client.post("/media/1/points", json={"thumbnail_id": 1})
    media_point_delete_response = client.delete("/media/1/points/1")
    delete_response = client.delete("/media/1")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert thumbnails_response.status_code == 401
    assert thumbnails_response.json()["error"]["code"] == "unauthorized"
    assert validity_check_response.status_code == 401
    assert validity_check_response.json()["error"]["code"] == "unauthorized"
    assert media_points_response.status_code == 401
    assert media_points_response.json()["error"]["code"] == "unauthorized"
    assert media_point_list_response.status_code == 401
    assert media_point_list_response.json()["error"]["code"] == "unauthorized"
    assert media_point_create_response.status_code == 401
    assert media_point_create_response.json()["error"]["code"] == "unauthorized"
    assert media_point_delete_response.status_code == 401
    assert media_point_delete_response.json()["error"]["code"] == "unauthorized"
    assert delete_response.status_code == 401
    assert delete_response.json()["error"]["code"] == "unauthorized"


def test_check_media_validity_invalidates_missing_file(client, account_user, tmp_path):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-040", "MovieA40", title="Movie 40")
    missing_path = tmp_path / "missing.mp4"
    media = Media.create(movie=movie, path=str(missing_path), valid=True)

    response = client.post(
        f"/media/{media.id}/validity-check",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == media.id
    assert payload["path"] == str(missing_path)
    assert payload["file_exists"] is False
    assert payload["valid_before"] is True
    assert payload["valid_after"] is False
    assert payload["updated"] is True
    assert payload["invalidated"] is True
    assert payload["revived"] is False
    assert "Z" not in payload["checked_at"]
    assert Media.get_by_id(media.id).valid is False


def test_check_media_validity_revives_invalid_media_and_removes_from_invalid_list(
    client,
    account_user,
    tmp_path,
):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-041", "MovieA41", title="Movie 41")
    file_path = tmp_path / "abc-041.mp4"
    file_path.write_bytes(b"video-bytes")
    media = Media.create(
        movie=movie,
        path=str(file_path),
        valid=False,
        video_info={"container": {"format_name": "mp4"}, "video": None, "audio": None, "subtitles": []},
    )

    before_response = client.get(
        "/media/invalid",
        headers={"Authorization": f"Bearer {token}"},
    )
    check_response = client.post(
        f"/media/{media.id}/validity-check",
        headers={"Authorization": f"Bearer {token}"},
    )
    after_response = client.get(
        "/media/invalid",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert before_response.status_code == 200
    assert before_response.json()["total"] == 1
    assert check_response.status_code == 200
    payload = check_response.json()
    assert payload["file_exists"] is True
    assert payload["valid_before"] is False
    assert payload["valid_after"] is True
    assert payload["updated"] is True
    assert payload["invalidated"] is False
    assert payload["revived"] is True
    assert after_response.status_code == 200
    assert after_response.json()["total"] == 0
    assert Media.get_by_id(media.id).valid is True


def test_list_invalid_media_returns_movie_cover_images(client, account_user):
    token = _login(client, username=account_user.username)
    cover_image = _create_image("movies/ABC-042/cover.webp")
    thin_cover_image = _create_image("movies/ABC-042/thin-cover.webp")
    movie = _create_movie(
        "ABC-042",
        "MovieA42",
        title="Movie 42",
        cover_image=cover_image,
        thin_cover_image=thin_cover_image,
    )
    media = Media.create(movie=movie, path="/library/main/abc-042.mp4", valid=False)

    response = client.get(
        "/media/invalid",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["id"] == media.id
    assert item["cover_image"]["id"] == cover_image.id
    assert item["thin_cover_image"]["id"] == thin_cover_image.id
    assert item["cover_image"]["medium"].startswith("/files/images/")
    assert item["thin_cover_image"]["medium"].startswith("/files/images/")


def test_list_invalid_media_returns_null_cover_images_when_missing(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-043", "MovieA43", title="Movie 43")
    Media.create(movie=movie, path="/library/main/abc-043.mp4", valid=False)

    response = client.get(
        "/media/invalid",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["cover_image"] is None
    assert item["thin_cover_image"] is None


def test_list_invalid_cloud115_media_uses_locator_display_path(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-115", "MovieCloud115", title="Cloud Movie")
    library = MediaLibrary.create(
        name="Cloud",
        backend="cloud115",
        backend_account_key="cloud115:invalid-list",
        backend_config={"cookies": "UID=invalid-list_A1_x", "root_cid": "root", "app": "web"},
    )
    media = Media.create(
        movie=movie,
        library=library,
        backend_locator={
            "fid": "fid-115",
            "pickcode": "pc-115",
            "name": "ABC-115.mp4",
            "source_path": "incoming/ABC-115.mp4",
        },
        content_fingerprint="sha1:CLOUDINVALID",
        valid=False,
    )

    response = client.get(
        "/media/invalid",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    item = next(item for item in response.json()["items"] if item["id"] == media.id)
    assert item["path"] == "cloud115:ABC-115.mp4"


def test_check_media_validity_returns_not_found_for_missing_media(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/media/999/validity-check",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "media_not_found"

def test_list_media_thumbnails_returns_expected_payload(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-008", "MovieA8", title="Movie 8")
    media = Media.create(
        movie=movie,
        path="/library/main/abc-008.mp4",
        valid=True,
        resolution="1280x720",
    )
    second_image = Image.create(
        origin="movies/ABC-008/media/fingerprint-1/thumbnails/20.webp",
        small="movies/ABC-008/media/fingerprint-1/thumbnails/20.webp",
        medium="movies/ABC-008/media/fingerprint-1/thumbnails/20.webp",
        large="movies/ABC-008/media/fingerprint-1/thumbnails/20.webp",
    )
    first_image = Image.create(
        origin="movies/ABC-008/media/fingerprint-1/thumbnails/10.webp",
        small="movies/ABC-008/media/fingerprint-1/thumbnails/10.webp",
        medium="movies/ABC-008/media/fingerprint-1/thumbnails/10.webp",
        large="movies/ABC-008/media/fingerprint-1/thumbnails/10.webp",
    )
    second_thumbnail = MediaThumbnail.create(media=media, image=second_image, offset=20)
    first_thumbnail = MediaThumbnail.create(media=media, image=first_image, offset=10)

    response = client.get(
        f"/media/{media.id}/thumbnails",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["thumbnail_id"] for item in payload] == [first_thumbnail.id, second_thumbnail.id]
    assert [item["offset_seconds"] for item in payload] == [10, 20]
    assert [item["media_id"] for item in payload] == [media.id, media.id]
    assert payload[0]["image"]["id"] == first_image.id
    assert payload[1]["image"]["id"] == second_image.id
    assert payload[0]["image"]["origin"].startswith("/files/images/")
    assert payload[0]["image"]["small"].startswith("/files/images/")
    assert payload[0]["image"]["medium"].startswith("/files/images/")
    assert payload[0]["image"]["large"].startswith("/files/images/")
    # 缩略图无缩放，尺寸复用所属媒体分辨率，整组一致。
    assert [item["width"] for item in payload] == [1280, 1280]
    assert [item["height"] for item in payload] == [720, 720]


def test_list_media_thumbnails_returns_expected_errors(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-009", "MovieA9", title="Movie 9")
    media = Media.create(movie=movie, path="/library/main/abc-009.mp4", valid=True)

    missing_response = client.get(
        "/media/999/thumbnails",
        headers={"Authorization": f"Bearer {token}"},
    )
    empty_response = client.get(
        f"/media/{media.id}/thumbnails",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "media_not_found"
    assert empty_response.status_code == 200
    assert empty_response.json() == []


def test_stream_media_returns_file_when_signature_is_valid(client, tmp_path, build_signed_media_url):
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    video_path = tmp_path / "ABC-001.mp4"
    video_path.write_bytes(b"video-bytes")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

    response = client.get(build_signed_media_url(media.id))

    assert response.status_code == 200
    assert response.content == b"video-bytes"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(len(b"video-bytes"))


def test_stream_media_returns_partial_content_for_range_requests(client, tmp_path, build_signed_media_url):
    movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    video_path = tmp_path / "ABC-002.mp4"
    video_path.write_bytes(b"video-bytes")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

    response = client.get(
        build_signed_media_url(media.id),
        headers={"Range": "bytes=0-3"},
    )

    assert response.status_code == 206
    assert response.content == b"vide"
    assert response.headers["content-range"] == f"bytes 0-3/{len(b'video-bytes')}"
    assert response.headers["content-length"] == "4"
    assert response.headers["accept-ranges"] == "bytes"


def test_stream_media_rejects_missing_signature(client):
    response = client.get("/media/1/stream")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_invalid"


def test_stream_media_rejects_expired_signature(client, tmp_path, build_signed_media_url):
    movie = _create_movie("ABC-003", "MovieA3", title="Movie 3")
    video_path = tmp_path / "ABC-003.mp4"
    video_path.write_bytes(b"video")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

    response = client.get(build_signed_media_url(media.id, expires=1700000900 - 901))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_expired"


def test_stream_media_rejects_signature_reuse_for_different_media(client, tmp_path, build_signed_media_url):
    first_movie = _create_movie("ABC-004", "MovieA4", title="Movie 4")
    second_movie = _create_movie("ABC-005", "MovieA5", title="Movie 5")
    first_path = tmp_path / "ABC-004.mp4"
    second_path = tmp_path / "ABC-005.mp4"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first_media = Media.create(movie=first_movie, path=str(first_path), valid=True)
    second_media = Media.create(movie=second_movie, path=str(second_path), valid=True)

    response = client.get(
        build_signed_media_url(first_media.id).replace(
            f"/media/{first_media.id}/stream",
            f"/media/{second_media.id}/stream",
        )
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_invalid"


def test_stream_media_rejects_invalid_range(client, tmp_path, build_signed_media_url):
    movie = _create_movie("ABC-006", "MovieA6", title="Movie 6")
    video_path = tmp_path / "ABC-006.mp4"
    video_path.write_bytes(b"video")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

    response = client.get(
        build_signed_media_url(media.id),
        headers={"Range": "bytes=10-1"},
    )

    assert response.status_code == 416


def test_stream_media_returns_not_found_for_missing_media(client, build_signed_media_url):
    response = client.get(build_signed_media_url(999))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "media_not_found"


def test_stream_media_returns_not_found_when_file_is_missing(client, tmp_path, build_signed_media_url):
    movie = _create_movie("ABC-007", "MovieA7", title="Movie 7")
    media = Media.create(movie=movie, path=str(tmp_path / "missing.mp4"), valid=True)

    response = client.get(build_signed_media_url(media.id))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "file_not_found"


def test_update_media_progress_creates_progress_and_recently_played(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    media = Media.create(movie=movie, path="/library/main/abc-001.mp4", valid=True)
    watched_at = datetime(2026, 3, 12, 14, 0, 0)

    monkeypatch.setattr(MediaService, "_current_time", lambda: watched_at)
    monkeypatch.setattr(PlaylistService, "_current_time", lambda: watched_at)

    response = client.put(
        f"/media/{media.id}/progress",
        headers={"Authorization": f"Bearer {token}"},
        json={"position_seconds": 600},
    )

    progress = MediaProgress.get(MediaProgress.media == media)
    playlist = Playlist.get(Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED)
    playlist_movie = PlaylistMovie.get(PlaylistMovie.playlist == playlist, PlaylistMovie.movie == movie)

    assert response.status_code == 200
    assert response.json() == {
        "media_id": media.id,
        "last_position_seconds": 600,
        "last_watched_at": "2026-03-12T14:00:00",
    }
    assert progress.position_seconds == 600
    assert playlist_movie.updated_at == watched_at


def test_update_media_progress_updates_existing_progress(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    media = Media.create(movie=movie, path="/library/main/abc-001.mp4", valid=True)
    playlist = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
        description="系统自动维护的最近播放影片列表",
    )
    PlaylistMovie.create(playlist=playlist, movie=movie)
    MediaProgress.create(
        media=media,
        position_seconds=100,
        last_watched_at="2026-03-12 13:00:00",
    )
    watched_at = datetime(2026, 3, 12, 15, 0, 0)

    monkeypatch.setattr(MediaService, "_current_time", lambda: watched_at)
    monkeypatch.setattr(PlaylistService, "_current_time", lambda: watched_at)

    response = client.put(
        f"/media/{media.id}/progress",
        headers={"Authorization": f"Bearer {token}"},
        json={"position_seconds": 900},
    )

    progress = MediaProgress.get(MediaProgress.media == media)
    playlist_movie = PlaylistMovie.get(PlaylistMovie.playlist == playlist, PlaylistMovie.movie == movie)

    assert response.status_code == 200
    assert progress.position_seconds == 900
    assert progress.last_watched_at == watched_at
    assert playlist_movie.updated_at == watched_at


def test_update_media_progress_returns_expected_errors(client, account_user):
    token = _login(client, username=account_user.username)

    missing_response = client.put(
        "/media/999/progress",
        headers={"Authorization": f"Bearer {token}"},
        json={"position_seconds": 600},
    )
    invalid_response = client.put(
        "/media/999/progress",
        headers={"Authorization": f"Bearer {token}"},
        json={"position_seconds": -1},
    )

    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "media_not_found"
    assert invalid_response.status_code == 422
    assert invalid_response.json()["error"]["code"] == "validation_error"


def test_list_media_points_returns_paginated_results_sorted_by_created_at(
    client,
    account_user,
    build_signed_image_url,
):
    token = _login(client, username=account_user.username)
    first_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    second_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    first_media = Media.create(movie=first_movie, path="/library/main/abc-001.mp4", valid=True)
    second_media = Media.create(movie=second_movie, path="/library/main/abc-002.mp4", valid=True)
    older_thumbnail = _create_thumbnail(first_media, offset_seconds=120, suffix="older")
    newer_thumbnail = _create_thumbnail(second_media, offset_seconds=360, suffix="newer")
    older_point = MediaPoint.create(
        media=first_media,
        thumbnail=older_thumbnail,
        offset_seconds=120,
        created_at=datetime(2026, 3, 12, 10, 0, 0),
        updated_at=datetime(2026, 3, 12, 10, 0, 0),
    )
    newer_point = MediaPoint.create(
        media=second_media,
        thumbnail=newer_thumbnail,
        offset_seconds=360,
        created_at=datetime(2026, 3, 12, 11, 0, 0),
        updated_at=datetime(2026, 3, 12, 11, 0, 0),
    )

    desc_response = client.get(
        "/media-points?page=1&page_size=20",
        headers={"Authorization": f"Bearer {token}"},
    )
    asc_response = client.get(
        "/media-points?page=1&page_size=20&sort=created_at:asc",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert desc_response.status_code == 200
    assert desc_response.json() == {
        "items": [
            {
                "point_id": newer_point.id,
                "media_id": second_media.id,
                "movie_number": "ABC-002",
                "video_item_id": None,
                "thumbnail_id": newer_thumbnail.id,
                "offset_seconds": 360,
                "image": {
                    "id": newer_thumbnail.image_id,
                    "origin": build_signed_image_url(newer_thumbnail.image.origin),
                    "small": build_signed_image_url(newer_thumbnail.image.small),
                    "medium": build_signed_image_url(newer_thumbnail.image.medium),
                    "large": build_signed_image_url(newer_thumbnail.image.large),
                },
                "created_at": "2026-03-12T11:00:00",
            },
            {
                "point_id": older_point.id,
                "media_id": first_media.id,
                "movie_number": "ABC-001",
                "video_item_id": None,
                "thumbnail_id": older_thumbnail.id,
                "offset_seconds": 120,
                "image": {
                    "id": older_thumbnail.image_id,
                    "origin": build_signed_image_url(older_thumbnail.image.origin),
                    "small": build_signed_image_url(older_thumbnail.image.small),
                    "medium": build_signed_image_url(older_thumbnail.image.medium),
                    "large": build_signed_image_url(older_thumbnail.image.large),
                },
                "created_at": "2026-03-12T10:00:00",
            },
        ],
        "page": 1,
        "page_size": 20,
        "total": 2,
    }
    assert asc_response.status_code == 200
    assert [item["point_id"] for item in asc_response.json()["items"]] == [older_point.id, newer_point.id]


def test_list_media_points_defaults_to_jav_and_filters_by_kind(client, account_user):
    token = _login(client, username=account_user.username)
    # 一条 JAV 时刻 + 一条非 JAV(video_item) 时刻，验证 kind 过滤与默认值。
    movie = _create_movie("ABC-100", "MovieA100", title="Movie 100")
    jav_media = Media.create(movie=movie, path="/library/main/abc-100.mp4", valid=True)
    jav_thumbnail = _create_thumbnail(jav_media, offset_seconds=120, suffix="jav")
    jav_point = MediaPoint.create(media=jav_media, thumbnail=jav_thumbnail, offset_seconds=120)

    video_item = VideoItem.create(title="家庭录像")
    video_media = Media.create(video_item=video_item, path="/library/main/home.mp4", valid=True)
    video_image = _create_image("videos/1/media/fp/thumbnails/30.webp")
    video_thumbnail = MediaThumbnail.create(media=video_media, image=video_image, offset=30)
    video_point = MediaPoint.create(media=video_media, thumbnail=video_thumbnail, offset_seconds=30)

    headers = {"Authorization": f"Bearer {token}"}
    default_response = client.get("/media-points", headers=headers)
    jav_response = client.get("/media-points?kind=jav", headers=headers)
    video_response = client.get("/media-points?kind=video", headers=headers)
    all_response = client.get("/media-points?kind=all", headers=headers)

    # 默认只返回 JAV 时刻。
    assert default_response.status_code == 200
    default_body = default_response.json()
    assert default_body["total"] == 1
    assert [item["point_id"] for item in default_body["items"]] == [jav_point.id]
    assert default_body["items"][0]["movie_number"] == "ABC-100"

    assert [item["point_id"] for item in jav_response.json()["items"]] == [jav_point.id]

    video_body = video_response.json()
    assert video_body["total"] == 1
    assert [item["point_id"] for item in video_body["items"]] == [video_point.id]
    assert video_body["items"][0]["movie_number"] is None
    assert video_body["items"][0]["video_item_id"] == video_item.id

    all_body = all_response.json()
    assert all_body["total"] == 2
    assert {item["point_id"] for item in all_body["items"]} == {jav_point.id, video_point.id}


def test_list_media_points_rejects_invalid_kind(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/media-points?kind=unknown",
        headers={"Authorization": f"Bearer {token}"},
    )

    # kind 为枚举，非法值由 FastAPI 校验拦截返回 422。
    assert response.status_code == 422


def test_list_media_points_rejects_invalid_sort(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    media = Media.create(movie=movie, path="/library/main/abc-001.mp4", valid=True)
    thumbnail = _create_thumbnail(media, offset_seconds=120)
    MediaPoint.create(media=media, thumbnail=thumbnail, offset_seconds=120)

    response = client.get(
        "/media-points?sort=id:desc",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_media_point_filter"


def test_list_media_points_for_media_returns_points_sorted_by_point_id(
    client,
    account_user,
    build_signed_image_url,
):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-010", "MovieA10", title="Movie 10")
    media = Media.create(movie=movie, path="/library/main/abc-010.mp4", valid=True)
    backup_media = Media.create(movie=movie, path="/library/main/abc-010-backup.mp4", valid=True)
    first_thumbnail = _create_thumbnail(media, offset_seconds=360, suffix="first")
    second_thumbnail = _create_thumbnail(media, offset_seconds=120, suffix="second")
    backup_thumbnail = _create_thumbnail(backup_media, offset_seconds=90, suffix="backup")
    first_point = MediaPoint.create(media=media, thumbnail=first_thumbnail, offset_seconds=360)
    second_point = MediaPoint.create(media=media, thumbnail=second_thumbnail, offset_seconds=120)
    MediaPoint.create(media=backup_media, thumbnail=backup_thumbnail, offset_seconds=90)

    response = client.get(
        f"/media/{media.id}/points",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "point_id": first_point.id,
            "media_id": media.id,
            "thumbnail_id": first_thumbnail.id,
            "offset_seconds": 360,
            "image": {
                "id": first_thumbnail.image_id,
                "origin": build_signed_image_url(first_thumbnail.image.origin),
                "small": build_signed_image_url(first_thumbnail.image.small),
                "medium": build_signed_image_url(first_thumbnail.image.medium),
                "large": build_signed_image_url(first_thumbnail.image.large),
            },
            "created_at": first_point.created_at.isoformat(timespec="seconds"),
        },
        {
            "point_id": second_point.id,
            "media_id": media.id,
            "thumbnail_id": second_thumbnail.id,
            "offset_seconds": 120,
            "image": {
                "id": second_thumbnail.image_id,
                "origin": build_signed_image_url(second_thumbnail.image.origin),
                "small": build_signed_image_url(second_thumbnail.image.small),
                "medium": build_signed_image_url(second_thumbnail.image.medium),
                "large": build_signed_image_url(second_thumbnail.image.large),
            },
            "created_at": second_point.created_at.isoformat(timespec="seconds"),
        },
    ]


def test_list_media_points_for_media_returns_empty_list_when_no_points(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-011", "MovieA11", title="Movie 11")
    media = Media.create(movie=movie, path="/library/main/abc-011.mp4", valid=True)

    response = client.get(
        f"/media/{media.id}/points",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_create_media_point_returns_created_resource(client, account_user, build_signed_image_url):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-012", "MovieA12", title="Movie 12")
    media = Media.create(movie=movie, path="/library/main/abc-012.mp4", valid=True)
    thumbnail = _create_thumbnail(media, offset_seconds=120, suffix="create")

    response = client.post(
        f"/media/{media.id}/points",
        headers={"Authorization": f"Bearer {token}"},
        json={"thumbnail_id": thumbnail.id},
    )

    point = MediaPoint.get(MediaPoint.media == media, MediaPoint.thumbnail == thumbnail)

    assert response.status_code == 201
    assert response.json() == {
        "point_id": point.id,
        "media_id": media.id,
        "thumbnail_id": thumbnail.id,
        "offset_seconds": 120,
        "image": {
            "id": thumbnail.image_id,
            "origin": build_signed_image_url(thumbnail.image.origin),
            "small": build_signed_image_url(thumbnail.image.small),
            "medium": build_signed_image_url(thumbnail.image.medium),
            "large": build_signed_image_url(thumbnail.image.large),
        },
        "created_at": point.created_at.isoformat(timespec="seconds"),
    }


def test_create_media_point_returns_existing_resource_for_duplicate_thumbnail(
    client,
    account_user,
    build_signed_image_url,
):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-013", "MovieA13", title="Movie 13")
    media = Media.create(movie=movie, path="/library/main/abc-013.mp4", valid=True)
    thumbnail = _create_thumbnail(media, offset_seconds=300, suffix="duplicate")
    existing_point = MediaPoint.create(media=media, thumbnail=thumbnail, offset_seconds=300)

    response = client.post(
        f"/media/{media.id}/points",
        headers={"Authorization": f"Bearer {token}"},
        json={"thumbnail_id": thumbnail.id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "point_id": existing_point.id,
        "media_id": media.id,
        "thumbnail_id": thumbnail.id,
        "offset_seconds": 300,
        "image": {
            "id": thumbnail.image_id,
            "origin": build_signed_image_url(thumbnail.image.origin),
            "small": build_signed_image_url(thumbnail.image.small),
            "medium": build_signed_image_url(thumbnail.image.medium),
            "large": build_signed_image_url(thumbnail.image.large),
        },
        "created_at": existing_point.created_at.isoformat(timespec="seconds"),
    }
    assert (
        MediaPoint.select()
        .where(MediaPoint.media == media, MediaPoint.thumbnail == thumbnail)
        .count()
        == 1
    )


def test_create_media_point_returns_expected_errors(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-017", "MovieA17", title="Movie 17")
    media = Media.create(movie=movie, path="/library/main/abc-017.mp4", valid=True)
    backup_media = Media.create(movie=movie, path="/library/main/abc-017-backup.mp4", valid=True)
    backup_thumbnail = _create_thumbnail(backup_media, offset_seconds=240, suffix="mismatch")

    missing_response = client.post(
        "/media/999/points",
        headers={"Authorization": f"Bearer {token}"},
        json={"thumbnail_id": 1},
    )
    invalid_response = client.post(
        f"/media/{media.id}/points",
        headers={"Authorization": f"Bearer {token}"},
        json={"thumbnail_id": 0},
    )
    mismatched_thumbnail_response = client.post(
        f"/media/{media.id}/points",
        headers={"Authorization": f"Bearer {token}"},
        json={"thumbnail_id": backup_thumbnail.id},
    )

    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "media_not_found"
    assert invalid_response.status_code == 422
    assert invalid_response.json()["error"]["code"] == "validation_error"
    assert mismatched_thumbnail_response.status_code == 404
    assert mismatched_thumbnail_response.json()["error"]["code"] == "media_thumbnail_not_found"


def test_delete_media_point_removes_single_point(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-014", "MovieA14", title="Movie 14")
    media = Media.create(movie=movie, path="/library/main/abc-014.mp4", valid=True)
    thumbnail = _create_thumbnail(media, offset_seconds=180, suffix="delete")
    point = MediaPoint.create(media=media, thumbnail=thumbnail, offset_seconds=180)

    response = client.delete(
        f"/media/{media.id}/points/{point.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert MediaPoint.get_or_none(MediaPoint.id == point.id) is None


def test_delete_media_point_returns_not_found_for_missing_or_mismatched_point(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-015", "MovieA15", title="Movie 15")
    media = Media.create(movie=movie, path="/library/main/abc-015.mp4", valid=True)
    backup_media = Media.create(movie=movie, path="/library/main/abc-015-backup.mp4", valid=True)
    backup_thumbnail = _create_thumbnail(backup_media, offset_seconds=240, suffix="delete-mismatch")
    point = MediaPoint.create(media=backup_media, thumbnail=backup_thumbnail, offset_seconds=240)

    missing_response = client.delete(
        f"/media/{media.id}/points/999",
        headers={"Authorization": f"Bearer {token}"},
    )
    mismatched_response = client.delete(
        f"/media/{media.id}/points/{point.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "media_point_not_found"
    assert mismatched_response.status_code == 404
    assert mismatched_response.json()["error"]["code"] == "media_point_not_found"
    assert MediaPoint.get_by_id(point.id).media_id == backup_media.id


def test_media_point_endpoints_update_movie_detail_points(client, account_user, build_signed_image_url):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-016", "MovieA16", title="Movie 16")
    media = Media.create(movie=movie, path="/library/main/abc-016.mp4", valid=True)
    thumbnail = _create_thumbnail(media, offset_seconds=480, suffix="detail")

    create_response = client.post(
        f"/media/{media.id}/points",
        headers={"Authorization": f"Bearer {token}"},
        json={"thumbnail_id": thumbnail.id},
    )

    created_point_id = create_response.json()["point_id"]
    detail_after_create = client.get(
        f"/movies/{movie.movie_number}",
        headers={"Authorization": f"Bearer {token}"},
    )
    delete_response = client.delete(
        f"/media/{media.id}/points/{created_point_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    detail_after_delete = client.get(
        f"/movies/{movie.movie_number}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert create_response.status_code == 201
    assert detail_after_create.status_code == 200
    assert detail_after_create.json()["media_items"][0]["points"] == [
        {
            "point_id": created_point_id,
            "thumbnail_id": thumbnail.id,
            "offset_seconds": 480,
            "image": {
                "id": thumbnail.image_id,
                "origin": build_signed_image_url(thumbnail.image.origin),
                "small": build_signed_image_url(thumbnail.image.small),
                "medium": build_signed_image_url(thumbnail.image.medium),
                "large": build_signed_image_url(thumbnail.image.large),
            },
        }
    ]
    assert delete_response.status_code == 204
    assert detail_after_delete.status_code == 200
    assert detail_after_delete.json()["media_items"][0]["points"] == []


def test_delete_media_hard_deletes_media_and_cleans_related_records(client, account_user, tmp_path, monkeypatch):
    token = _login(client, username=account_user.username)
    monkeypatch.setattr(
        "src.service.catalog.image_cleanup_service.ImageCleanupService.image_root_path",
        classmethod(lambda cls: tmp_path),
    )
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    file_path = tmp_path / "abc-001.mp4"
    file_path.write_bytes(b"media")
    media = Media.create(movie=movie, path=str(file_path), valid=True)
    progress = MediaProgress.create(media=media, position_seconds=100, last_watched_at=datetime(2026, 3, 12, 10, 0, 0))
    thumbnail_path = tmp_path / "thumb.jpg"
    thumbnail_path.write_bytes(b"thumb")
    image = Image.create(origin="thumb.jpg", small="thumb.jpg", medium="thumb.jpg", large="thumb.jpg")
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=60)
    point = MediaPoint.create(media=media, thumbnail=thumbnail, offset_seconds=120)
    task_state = ResourceTaskState.create(
        task_key="media_thumbnail_generation",
        resource_type="media",
        resource_id=media.id,
        state="succeeded",
    )
    custom_playlist = Playlist.create(name="我的收藏", description="Favorite")
    recent_playlist = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
        description="系统自动维护的最近播放影片列表",
    )
    PlaylistMovie.create(playlist=custom_playlist, movie=movie)
    PlaylistMovie.create(playlist=recent_playlist, movie=movie)
    deleted_media_ids = []
    monkeypatch.setattr(
        "src.service.playback.media_service.get_qdrant_thumbnail_store",
        lambda: type("Store", (), {"delete_by_media_id": lambda self, media_id: deleted_media_ids.append(media_id)})(),
    )

    response = client.delete(
        f"/media/{media.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert file_path.exists() is False
    assert thumbnail_path.exists() is False
    assert Media.get_or_none(Media.id == media.id) is None
    assert MediaProgress.get_or_none(MediaProgress.id == progress.id) is None
    assert MediaPoint.get_or_none(MediaPoint.id == point.id) is None
    assert MediaThumbnail.get_or_none(MediaThumbnail.id == thumbnail.id) is None
    assert Image.get_or_none(Image.id == image.id) is None
    assert ResourceTaskState.get_or_none(ResourceTaskState.id == task_state.id) is None
    assert Movie.get_by_id(movie.id).movie_number == "ABC-001"
    assert PlaylistMovie.select().count() == 2
    assert deleted_media_ids == [media.id]


def test_delete_media_keeps_decoupled_clips_and_collection_membership(
    client, account_user, tmp_path, monkeypatch
):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-050", "MovieA50", title="Movie 50")
    file_path = tmp_path / "abc-050.mp4"
    file_path.write_bytes(b"media")
    media = Media.create(movie=movie, path=str(file_path), valid=True)
    # 片段是独立资产：直接落库一条，不经过 ffmpeg。
    clip = MediaClip.create(
        media=media,
        movie_number=movie.movie_number,
        start_offset_seconds=10,
        end_offset_seconds=30,
        title="片段",
        file_path=f"{movie.movie_number}/1.mp4",
        file_size_bytes=10,
        duration_seconds=20,
    )
    collection = ClipCollection.create(name="连播合集")
    ClipCollectionItem.create(collection=collection, clip=clip, position=0)
    monkeypatch.setattr(
        "src.service.playback.media_service.get_qdrant_thumbnail_store",
        lambda: type("Store", (), {"delete_by_media_id": lambda self, _media_id: None})(),
    )

    response = client.delete(
        f"/media/{media.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert Media.get_or_none(Media.id == media.id) is None
    # 片段记录保留，仅来源引用置空；番号快照与合集成员关系不受影响。
    surviving = MediaClip.get_or_none(MediaClip.id == clip.id)
    assert surviving is not None
    assert surviving.media_id is None
    assert surviving.movie_number == "ABC-050"
    assert ClipCollectionItem.select().where(ClipCollectionItem.clip == clip.id).count() == 1


def test_delete_media_succeeds_when_file_is_missing(client, account_user, tmp_path, monkeypatch):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    media = Media.create(movie=movie, path=str(tmp_path / "missing.mp4"), valid=True)
    monkeypatch.setattr(
        "src.service.playback.media_service.get_qdrant_thumbnail_store",
        lambda: type("Store", (), {"delete_by_media_id": lambda self, _media_id: None})(),
    )

    response = client.delete(
        f"/media/{media.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert Media.get_or_none(Media.id == media.id) is None


def test_delete_media_returns_not_found_for_missing_media(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.delete(
        "/media/999",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "media_not_found"


def test_stream_cloud115_media_redirects_to_direct_url(
    client, build_signed_media_url, monkeypatch
):
    """cloud115 媒体：/stream 现拿绑定请求 UA 的直链后 302，不做本地 Range。"""
    from src.lib.cloud115.types import DirectUrl
    from src.service.playback import cloud115_backend_service as backend_module

    library = MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_config={
            "cookies": "UID=12345678_A1_1700000000; CID=abc",
            "root_cid": "lib-root",
            "app": "alipaymini",
        },
    )
    movie = _create_movie("ABC-115", "MovieC115", title="Cloud Movie")
    media = Media.create(
        movie=movie,
        library=library,
        backend_locator={"fid": "f-1", "pickcode": "pc-115", "name": "ABC-115.mp4"},
        content_fingerprint="sha1:AAA",
        valid=True,
    )

    captured = {}

    class _FakeStreamClient:
        def __init__(self, cookies: str):
            self._cookies = cookies

        async def close(self):
            pass

        def snapshot_cookies(self):
            return self._cookies

        async def get_download_url(self, pickcode: str, user_agent: str):
            captured["pickcode"] = pickcode
            captured["user_agent"] = user_agent
            return DirectUrl(
                file_id="f-1", file_name="ABC-115.mp4", file_size=1, sha1="AAA",
                pickcode=pickcode, url="https://cdn.115.example/video.mp4?t=99",
                user_agent=user_agent, expires_at=99,
            )

    monkeypatch.setattr(backend_module, "Cloud115Client", _FakeStreamClient)

    response = client.get(
        build_signed_media_url(media.id),
        headers={"User-Agent": "SakuraMedia-TestPlayer/9.9"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.115.example/video.mp4?t=99"
    assert captured["pickcode"] == "pc-115"
    # 绑定的 UA 必须是请求方 UA（302 后播放器跟随请求 CDN 时 UA 天然一致）
    assert captured["user_agent"] == "SakuraMedia-TestPlayer/9.9"


def test_stream_cloud115_media_maps_not_found(client, build_signed_media_url, monkeypatch):
    from src.lib.cloud115 import Cloud115NotFoundError
    from src.service.playback import cloud115_backend_service as backend_module

    library = MediaLibrary.create(
        name="cloud-2",
        backend="cloud115",
        backend_config={
            "cookies": "UID=12345678_A1_1700000000; CID=abc",
            "root_cid": "lib-root",
            "app": "alipaymini",
        },
    )
    movie = _create_movie("ABC-116", "MovieC116", title="Gone Movie")
    media = Media.create(
        movie=movie,
        library=library,
        backend_locator={"fid": "f-2", "pickcode": "pc-gone", "name": "ABC-116.mp4"},
        valid=True,
    )

    class _GoneClient:
        def __init__(self, cookies: str):
            self._cookies = cookies

        async def close(self):
            pass

        def snapshot_cookies(self):
            return self._cookies

        async def get_download_url(self, pickcode: str, user_agent: str):
            raise Cloud115NotFoundError("banned or deleted")

    monkeypatch.setattr(backend_module, "Cloud115Client", _GoneClient)

    response = client.get(build_signed_media_url(media.id), follow_redirects=False)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "cloud115_dir_not_found"


def test_delete_cloud115_media_deletes_remote_file_and_records(
    client, account_user, monkeypatch
):
    """删除 cloud115 Media：SDK 删云端文件（按 locator.fid）+ 删记录，对齐本地删除语义。"""
    from contextlib import asynccontextmanager

    from src.service.playback import cloud115_backend_service as backend_module

    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(
        name="cloud-del",
        backend="cloud115",
        backend_config={
            "cookies": "UID=12345678_A1_1700000000; CID=abc",
            "root_cid": "lib-root",
            "app": "alipaymini",
        },
    )
    movie = _create_movie("ABC-118", "MovieC118", title="Delete Me")
    media = Media.create(
        movie=movie,
        library=library,
        backend_locator={"fid": "f-del", "pickcode": "pc-del", "name": "ABC-118.mp4"},
        valid=True,
    )
    deleted_fids = []

    class _DeleteClient:
        async def delete_files(self, fids, *, pid=None):
            deleted_fids.extend(fids)

    @asynccontextmanager
    async def fake_client_for(_library):
        yield _DeleteClient()

    monkeypatch.setattr(backend_module, "cloud115_client_for", fake_client_for)

    response = client.delete(
        f"/media/{media.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert deleted_fids == ["f-del"]
    assert Media.get_or_none(Media.id == media.id) is None


def test_delete_cloud115_media_tolerates_remote_already_gone(
    client, account_user, monkeypatch
):
    from contextlib import asynccontextmanager

    from src.lib.cloud115 import Cloud115NotFoundError
    from src.service.playback import cloud115_backend_service as backend_module

    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(
        name="cloud-del-2",
        backend="cloud115",
        backend_config={
            "cookies": "UID=12345678_A1_1700000000; CID=abc",
            "root_cid": "lib-root",
            "app": "alipaymini",
        },
    )
    movie = _create_movie("ABC-119", "MovieC119", title="Already Gone")
    media = Media.create(
        movie=movie,
        library=library,
        backend_locator={"fid": "f-gone", "pickcode": "pc-gone", "name": "ABC-119.mp4"},
        valid=True,
    )

    class _GoneClient:
        async def delete_files(self, fids, *, pid=None):
            raise Cloud115NotFoundError("already deleted")

    @asynccontextmanager
    async def fake_client_for(_library):
        yield _GoneClient()

    monkeypatch.setattr(backend_module, "cloud115_client_for", fake_client_for)

    response = client.delete(
        f"/media/{media.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    # 云端已经没了：容忍并继续删记录
    assert response.status_code == 204
    assert Media.get_or_none(Media.id == media.id) is None


def test_delete_cloud115_media_keeps_record_on_upstream_error(
    client, account_user, monkeypatch
):
    """cookies 失效等上游错误：不静默吞——记录保留，避免云端孤儿文件。"""
    from contextlib import asynccontextmanager

    from src.lib.cloud115 import Cloud115AuthError
    from src.service.playback import cloud115_backend_service as backend_module

    token = _login(client, username=account_user.username)
    library = MediaLibrary.create(
        name="cloud-del-3",
        backend="cloud115",
        backend_config={
            "cookies": "UID=12345678_A1_1700000000; CID=abc",
            "root_cid": "lib-root",
            "app": "alipaymini",
        },
    )
    movie = _create_movie("ABC-120", "MovieC120", title="Locked")
    media = Media.create(
        movie=movie,
        library=library,
        backend_locator={"fid": "f-lock", "pickcode": "pc-lock", "name": "ABC-120.mp4"},
        valid=True,
    )

    class _DeadClient:
        async def delete_files(self, fids, *, pid=None):
            raise Cloud115AuthError("cookies expired")

    @asynccontextmanager
    async def fake_client_for(_library):
        yield _DeadClient()

    monkeypatch.setattr(backend_module, "cloud115_client_for", fake_client_for)

    response = client.delete(
        f"/media/{media.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cloud115_cookies_invalid"
    assert Media.get_or_none(Media.id == media.id) is not None
