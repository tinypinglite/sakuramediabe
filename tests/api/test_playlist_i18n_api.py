"""系统播放列表名的接口级多语言行为。"""

from src.model import (
    FAVORITES_PLAYLIST_DESCRIPTION,
    FAVORITES_PLAYLIST_NAME,
    PLAYLIST_KIND_FAVORITES,
    Movie,
    Playlist,
    PlaylistMovie,
)


def _auth_headers(client, account_user, language: str | None = None):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    if language:
        headers["Accept-Language"] = language
    return headers


def _seed_playlists():
    favorites = Playlist.create(
        kind=PLAYLIST_KIND_FAVORITES,
        name=FAVORITES_PLAYLIST_NAME,
        description=FAVORITES_PLAYLIST_DESCRIPTION,
    )
    custom = Playlist.create(kind="custom", name="我的片单", description="自建列表")
    return favorites, custom


def test_playlists_list_localizes_system_names_for_english(client, account_user):
    favorites, custom = _seed_playlists()

    response = client.get(
        "/playlists",
        headers=_auth_headers(client, account_user, "en"),
    )

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()}
    assert items[favorites.id]["name"] == "Favorites"
    assert items[favorites.id]["description"] == "Movies you have favorited manually"
    assert items[custom.id]["name"] == "我的片单"
    assert items[custom.id]["description"] == "自建列表"


def test_playlists_list_keeps_chinese_names_without_english_header(
    client,
    account_user,
):
    favorites, _custom = _seed_playlists()

    response = client.get("/playlists", headers=_auth_headers(client, account_user))

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()}
    assert items[favorites.id]["name"] == FAVORITES_PLAYLIST_NAME
    assert items[favorites.id]["description"] == FAVORITES_PLAYLIST_DESCRIPTION


def test_movie_detail_localizes_system_playlist_names(client, account_user):
    favorites, _custom = _seed_playlists()
    movie = Movie.create(movie_number="PL-001", title="pl")
    PlaylistMovie.create(playlist=favorites, movie=movie)

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user, "en"),
    )

    assert response.status_code == 200
    playlists = response.json()["playlists"]
    assert [(item["name"], item["is_system"]) for item in playlists] == [
        ("Favorites", True)
    ]


def test_localized_system_name_is_reserved_for_user_playlists(client, account_user):
    response = client.post(
        "/playlists",
        json={"name": "Favorites"},
        headers=_auth_headers(client, account_user, "en"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "playlist_reserved_name"
