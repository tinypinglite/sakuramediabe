"""影片观看状态：进度摘要、列表筛选与手动标记。"""

from src.model import (
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Media,
    MediaLibrary,
    MediaProgress,
    Movie,
    Playlist,
    PlaylistMovie,
)


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _library() -> MediaLibrary:
    return MediaLibrary.create(
        name="watch-status-library",
        provider_key="demo",
        provider_config={},
    )


def _movie_with_media(
    number: str,
    *,
    library: MediaLibrary,
    duration_seconds: int,
    position_seconds: int | None = None,
) -> tuple[Movie, Media]:
    movie = Movie.create(movie_number=number, title=number)
    media = Media.create(
        movie=movie,
        library=library,
        file_name=f"{number}.mp4",
        duration_seconds=duration_seconds,
    )
    if position_seconds is not None:
        MediaProgress.create(media=media, position_seconds=position_seconds)
    return movie, media


def _numbers(payload: dict) -> set[str]:
    return {item["movie_number"] for item in payload["items"]}


def _item(payload: dict, movie_number: str) -> dict:
    return next(
        item for item in payload["items"] if item["movie_number"] == movie_number
    )


def test_list_exposes_watch_progress_and_status_filters(client, account_user):
    headers = _login(client, account_user.username)
    library = _library()
    # 长片剩 5 分钟：已看。
    _movie_with_media(
        "WATCH-FINISHED",
        library=library,
        duration_seconds=7200,
        position_seconds=6900,
    )
    # 长片看到一半：在看。
    _movie_with_media(
        "WATCH-WATCHING",
        library=library,
        duration_seconds=7200,
        position_seconds=3600,
    )
    # 短片剩 5 分钟但只看到 75%：不算已看，仍是在看。
    _movie_with_media(
        "WATCH-SHORT",
        library=library,
        duration_seconds=1200,
        position_seconds=900,
    )
    # 没有任何进度：未看。
    _movie_with_media(
        "WATCH-UNWATCHED",
        library=library,
        duration_seconds=7200,
    )
    # 开场 5 秒的进度按未看处理。
    _movie_with_media(
        "WATCH-OPENING",
        library=library,
        duration_seconds=7200,
        position_seconds=5,
    )

    response = client.get("/movies", headers=headers)
    assert response.status_code == 200
    body = response.json()
    finished = _item(body, "WATCH-FINISHED")["watch_progress"]
    assert finished["position_seconds"] == 6900
    assert finished["duration_seconds"] == 7200
    assert finished["last_watched_at"] is None
    assert finished["finished"] is True
    watching = _item(body, "WATCH-WATCHING")["watch_progress"]
    assert watching["finished"] is False
    assert _item(body, "WATCH-UNWATCHED")["watch_progress"] is None

    watched = client.get(
        "/movies", headers=headers, params={"watch_status": "watched"}
    ).json()
    assert _numbers(watched) == {"WATCH-FINISHED"}

    watching_body = client.get(
        "/movies", headers=headers, params={"watch_status": "watching"}
    ).json()
    assert _numbers(watching_body) == {"WATCH-WATCHING", "WATCH-SHORT"}

    unwatched = client.get(
        "/movies", headers=headers, params={"watch_status": "unwatched"}
    ).json()
    assert _numbers(unwatched) == {"WATCH-UNWATCHED", "WATCH-OPENING"}


def test_watch_status_rejects_invalid_value(client, account_user):
    headers = _login(client, account_user.username)
    response = client.get(
        "/movies", headers=headers, params={"watch_status": "finished"}
    )
    assert response.status_code == 422


def test_playlist_movies_support_watch_status_filter(client, account_user):
    headers = _login(client, account_user.username)
    library = _library()
    finished, _ = _movie_with_media(
        "PL-FINISHED",
        library=library,
        duration_seconds=7200,
        position_seconds=7000,
    )
    watching, _ = _movie_with_media(
        "PL-WATCHING",
        library=library,
        duration_seconds=7200,
        position_seconds=1800,
    )
    playlist = Playlist.create(
        name="最近播放",
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
    )
    PlaylistMovie.create(playlist=playlist, movie=finished)
    PlaylistMovie.create(playlist=playlist, movie=watching)

    response = client.get(
        f"/playlists/{playlist.id}/movies",
        headers=headers,
        params={"watch_status": "watching"},
    )
    assert response.status_code == 200
    assert _numbers(response.json()) == {"PL-WATCHING"}


def test_mark_watched_and_unwatched(client, account_user):
    headers = _login(client, account_user.username)
    library = _library()
    _movie, media = _movie_with_media(
        "MARK-001",
        library=library,
        duration_seconds=7200,
    )

    watched = client.put(
        "/movies/MARK-001/watch-state",
        headers=headers,
        json={"state": "watched"},
    )
    assert watched.status_code == 200
    assert watched.json() == {"movie_number": "MARK-001", "state": "watched"}
    progress = MediaProgress.get(MediaProgress.media == media)
    assert progress.position_seconds == 7200

    body = client.get(
        "/movies", headers=headers, params={"watch_status": "watched"}
    ).json()
    assert _numbers(body) == {"MARK-001"}

    unwatched = client.put(
        "/movies/MARK-001/watch-state",
        headers=headers,
        json={"state": "unwatched"},
    )
    assert unwatched.status_code == 200
    assert MediaProgress.get_or_none(MediaProgress.media == media) is None
    body = client.get(
        "/movies", headers=headers, params={"watch_status": "unwatched"}
    ).json()
    assert _numbers(body) == {"MARK-001"}


def test_mark_watched_does_not_touch_recently_played(client, account_user):
    headers = _login(client, account_user.username)
    library = _library()
    _, _ = _movie_with_media(
        "MARK-HISTORY",
        library=library,
        duration_seconds=7200,
    )

    response = client.put(
        "/movies/MARK-HISTORY/watch-state",
        headers=headers,
        json={"state": "watched"},
    )
    assert response.status_code == 200
    assert PlaylistMovie.select().count() == 0


def test_mark_watched_requires_media_and_duration(client, account_user):
    headers = _login(client, account_user.username)
    Movie.create(movie_number="MARK-NO-MEDIA", title="no media")
    library = _library()
    _movie_with_media(
        "MARK-NO-DURATION",
        library=library,
        duration_seconds=0,
    )

    no_media = client.put(
        "/movies/MARK-NO-MEDIA/watch-state",
        headers=headers,
        json={"state": "watched"},
    )
    assert no_media.status_code == 409
    assert no_media.json()["error"]["code"] == "movie_has_no_media"

    no_duration = client.put(
        "/movies/MARK-NO-DURATION/watch-state",
        headers=headers,
        json={"state": "watched"},
    )
    assert no_duration.status_code == 409
    assert no_duration.json()["error"]["code"] == "movie_duration_unavailable"


def test_watch_progress_uses_latest_media_and_movie_level_finished(
    client, account_user
):
    headers = _login(client, account_user.username)
    library = _library()
    movie = Movie.create(movie_number="MULTI-001", title="multi")
    older = Media.create(
        movie=movie,
        library=library,
        file_name="multi-older.mp4",
        duration_seconds=7200,
    )
    newer = Media.create(
        movie=movie,
        library=library,
        file_name="multi-newer.mp4",
        duration_seconds=7200,
    )
    from datetime import datetime, timedelta

    now = datetime(2026, 1, 2, 3, 4, 5)
    MediaProgress.create(
        media=older,
        position_seconds=7000,
        last_watched_at=now - timedelta(hours=1),
    )
    MediaProgress.create(
        media=newer,
        position_seconds=100,
        last_watched_at=now,
    )

    body = client.get("/movies", headers=headers).json()
    progress = _item(body, "MULTI-001")["watch_progress"]
    # 进度摘要仍取最近观看的媒体；finished 采用影片级口径。
    assert progress["media_id"] == newer.id
    assert progress["position_seconds"] == 100
    assert progress["finished"] is True

    # 任一版本看完即视为已看，因此不再属于「在看」。
    watched = client.get(
        "/movies", headers=headers, params={"watch_status": "watched"}
    ).json()
    assert _numbers(watched) == {"MULTI-001"}
    watching = client.get(
        "/movies", headers=headers, params={"watch_status": "watching"}
    ).json()
    assert "MULTI-001" not in _numbers(watching)
