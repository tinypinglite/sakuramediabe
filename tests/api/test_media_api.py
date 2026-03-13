from datetime import datetime

from src.model import (
    Image,
    Media,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    PlaylistMovie,
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


def test_update_media_progress_requires_authentication(client):
    response = client.put("/media/1/progress", json={"position_seconds": 600})
    thumbnails_response = client.get("/media/1/thumbnails")
    media_points_response = client.get("/media-points")
    delete_response = client.delete("/media/1")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert thumbnails_response.status_code == 401
    assert thumbnails_response.json()["error"]["code"] == "unauthorized"
    assert media_points_response.status_code == 401
    assert media_points_response.json()["error"]["code"] == "unauthorized"
    assert delete_response.status_code == 401
    assert delete_response.json()["error"]["code"] == "unauthorized"


def test_list_media_thumbnails_returns_expected_payload(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-008", "MovieA8", title="Movie 8")
    media = Media.create(movie=movie, path="/library/main/abc-008.mp4", valid=True)
    second_image = Image.create(
        origin="movies/ABC-008/media/fingerprint-1/thumbnails/00_00_20.webp",
        small="movies/ABC-008/media/fingerprint-1/thumbnails/00_00_20.webp",
        medium="movies/ABC-008/media/fingerprint-1/thumbnails/00_00_20.webp",
        large="movies/ABC-008/media/fingerprint-1/thumbnails/00_00_20.webp",
    )
    first_image = Image.create(
        origin="movies/ABC-008/media/fingerprint-1/thumbnails/00_00_10.webp",
        small="movies/ABC-008/media/fingerprint-1/thumbnails/00_00_10.webp",
        medium="movies/ABC-008/media/fingerprint-1/thumbnails/00_00_10.webp",
        large="movies/ABC-008/media/fingerprint-1/thumbnails/00_00_10.webp",
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


def test_list_media_points_returns_paginated_results_sorted_by_created_at(client, account_user):
    token = _login(client, username=account_user.username)
    first_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    second_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    first_media = Media.create(movie=first_movie, path="/library/main/abc-001.mp4", valid=True)
    second_media = Media.create(movie=second_movie, path="/library/main/abc-002.mp4", valid=True)
    older_point = MediaPoint.create(
        media=first_media,
        offset_seconds=120,
        created_at=datetime(2026, 3, 12, 10, 0, 0),
        updated_at=datetime(2026, 3, 12, 10, 0, 0),
    )
    newer_point = MediaPoint.create(
        media=second_media,
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
                "offset_seconds": 360,
                "created_at": "2026-03-12T11:00:00",
            },
            {
                "point_id": older_point.id,
                "media_id": first_media.id,
                "movie_number": "ABC-001",
                "offset_seconds": 120,
                "created_at": "2026-03-12T10:00:00",
            },
        ],
        "page": 1,
        "page_size": 20,
        "total": 2,
    }
    assert asc_response.status_code == 200
    assert [item["point_id"] for item in asc_response.json()["items"]] == [older_point.id, newer_point.id]


def test_list_media_points_rejects_invalid_sort(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    media = Media.create(movie=movie, path="/library/main/abc-001.mp4", valid=True)
    MediaPoint.create(media=media, offset_seconds=120)

    response = client.get(
        "/media-points?sort=id:desc",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_media_point_filter"


def test_delete_media_soft_deletes_media_and_cleans_related_records(client, account_user, tmp_path, monkeypatch):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    file_path = tmp_path / "abc-001.mp4"
    file_path.write_bytes(b"media")
    media = Media.create(movie=movie, path=str(file_path), valid=True)
    progress = MediaProgress.create(media=media, position_seconds=100, last_watched_at=datetime(2026, 3, 12, 10, 0, 0))
    point = MediaPoint.create(media=media, offset_seconds=120)
    image = Image.create(origin="thumb.jpg", small="thumb.jpg", medium="thumb.jpg", large="thumb.jpg")
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=60)
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
        "src.service.playback.media_service.get_lancedb_thumbnail_store",
        lambda: type("Store", (), {"delete_by_media_id": lambda self, media_id: deleted_media_ids.append(media_id)})(),
    )

    response = client.delete(
        f"/media/{media.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    refreshed_media = Media.get_by_id(media.id)
    assert response.status_code == 204
    assert file_path.exists() is False
    assert refreshed_media.valid is False
    assert MediaProgress.get_or_none(MediaProgress.id == progress.id) is None
    assert MediaPoint.get_or_none(MediaPoint.id == point.id) is None
    assert MediaThumbnail.get_by_id(thumbnail.id).media_id == media.id
    assert Movie.get_by_id(movie.id).movie_number == "ABC-001"
    assert PlaylistMovie.select().count() == 2
    assert deleted_media_ids == [media.id]


def test_delete_media_succeeds_when_file_is_missing(client, account_user, tmp_path):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    media = Media.create(movie=movie, path=str(tmp_path / "missing.mp4"), valid=True)

    response = client.delete(
        f"/media/{media.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert Media.get_by_id(media.id).valid is False


def test_delete_media_returns_not_found_for_missing_media(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.delete(
        "/media/999",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "media_not_found"
