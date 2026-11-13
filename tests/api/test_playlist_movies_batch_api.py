"""播放列表批量加入影片接口的回归测试。"""

import pytest

from src.model import (
    FAVORITES_PLAYLIST_NAME,
    PLAYLIST_KIND_FAVORITES,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Movie,
    Playlist,
    PlaylistMovie,
)


def _auth(client, account_user) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_movie(movie_number: str) -> Movie:
    return Movie.create(movie_number=movie_number, title=movie_number)


def test_batch_add_movies_is_idempotent_and_skips_missing(client, account_user):
    playlist = Playlist.create(name="批量片单")
    first = _create_movie("BATCH-001")
    second = _create_movie("BATCH-002")
    headers = _auth(client, account_user)

    response = client.post(
        f"/playlists/{playlist.id}/movies",
        headers=headers,
        json={
            "movie_numbers": [
                first.movie_number,
                "BATCH-MISSING",
                second.movie_number,
            ]
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "requested_count": 3,
        "updated_count": 2,
        "skipped_count": 1,
        "skipped": [
            {"movie_number": "BATCH-MISSING", "reason": "movie_not_found"}
        ],
    }
    assert PlaylistMovie.select().where(PlaylistMovie.playlist == playlist).count() == 2

    repeated = client.post(
        f"/playlists/{playlist.id}/movies",
        headers=headers,
        json={"movie_numbers": [first.movie_number, second.movie_number]},
    )

    assert repeated.status_code == 200
    assert repeated.json()["updated_count"] == 2
    assert PlaylistMovie.select().where(PlaylistMovie.playlist == playlist).count() == 2


def test_batch_add_movies_allows_favorites(client, account_user):
    favorites = Playlist.create(
        kind=PLAYLIST_KIND_FAVORITES,
        name=FAVORITES_PLAYLIST_NAME,
    )
    movie = _create_movie("BATCH-FAV-001")

    response = client.post(
        f"/playlists/{favorites.id}/movies",
        headers=_auth(client, account_user),
        json={"movie_numbers": [movie.movie_number]},
    )

    assert response.status_code == 200
    assert response.json()["updated_count"] == 1
    assert PlaylistMovie.get(
        PlaylistMovie.playlist == favorites,
        PlaylistMovie.movie == movie,
    )


def test_batch_add_movies_rejects_recently_played(client, account_user):
    recently_played = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
    )

    response = client.post(
        f"/playlists/{recently_played.id}/movies",
        headers=_auth(client, account_user),
        json={"movie_numbers": ["BATCH-RECENT-001"]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "playlist_managed_by_system"


@pytest.mark.parametrize(
    "movie_numbers",
    [
        [],
        [""],
        ["BATCH-001", "BATCH-001"],
    ],
)
def test_batch_add_movies_rejects_invalid_numbers(
    client,
    account_user,
    movie_numbers,
):
    playlist = Playlist.create(name="非法批量片单")

    response = client.post(
        f"/playlists/{playlist.id}/movies",
        headers=_auth(client, account_user),
        json={"movie_numbers": movie_numbers},
    )

    assert response.status_code == 422


def test_batch_add_movies_accepts_1000_and_rejects_1001(client, account_user):
    playlist = Playlist.create(name="上限片单")
    _create_movie("BATCH-LIMIT-0001")
    headers = _auth(client, account_user)
    accepted_numbers = [f"BATCH-LIMIT-{index:04d}" for index in range(1, 1001)]

    accepted = client.post(
        f"/playlists/{playlist.id}/movies",
        headers=headers,
        json={"movie_numbers": accepted_numbers},
    )

    assert accepted.status_code == 200
    assert accepted.json()["requested_count"] == 1000
    assert accepted.json()["updated_count"] == 1
    assert accepted.json()["skipped_count"] == 999

    rejected = client.post(
        f"/playlists/{playlist.id}/movies",
        headers=headers,
        json={
            "movie_numbers": [
                f"BATCH-LIMIT-{index:04d}" for index in range(1, 1002)
            ]
        },
    )

    assert rejected.status_code == 422
