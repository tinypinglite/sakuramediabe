from src.model import PLAYLIST_KIND_RECENTLY_PLAYED, Movie, Playlist, PlaylistMovie


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


def test_playlist_endpoints_require_authentication(client):
    create_response = client.post("/playlists", json={"name": "我的收藏", "description": ""})
    list_response = client.get("/playlists")
    get_response = client.get("/playlists/1")
    update_response = client.patch("/playlists/1", json={"name": "稍后再看"})
    delete_response = client.delete("/playlists/1")
    add_response = client.put("/playlists/1/movies/ABC-001")
    remove_response = client.delete("/playlists/1/movies/ABC-001")

    assert create_response.status_code == 401
    assert list_response.status_code == 401
    assert get_response.status_code == 401
    assert update_response.status_code == 401
    assert delete_response.status_code == 401
    assert add_response.status_code == 401
    assert remove_response.status_code == 401


def test_playlist_crud_and_movie_membership_flow(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")

    create_response = client.post(
        "/playlists",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": " 我的收藏 ", "description": " Favorite "},
    )

    playlist_id = create_response.json()["id"]

    add_response = client.put(
        f"/playlists/{playlist_id}/movies/{movie.movie_number}",
        headers={"Authorization": f"Bearer {token}"},
    )
    detail_response = client.get(
        f"/playlists/{playlist_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    list_movies_response = client.get(
        f"/playlists/{playlist_id}/movies",
        headers={"Authorization": f"Bearer {token}"},
    )
    update_response = client.patch(
        f"/playlists/{playlist_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "稍后再看", "description": "Need watch later"},
    )
    remove_response = client.delete(
        f"/playlists/{playlist_id}/movies/{movie.movie_number}",
        headers={"Authorization": f"Bearer {token}"},
    )
    delete_response = client.delete(
        f"/playlists/{playlist_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert create_response.status_code == 201
    assert create_response.json()["name"] == "我的收藏"
    assert create_response.json()["kind"] == "custom"
    assert create_response.json()["movie_count"] == 0
    assert add_response.status_code == 204
    assert detail_response.status_code == 200
    assert detail_response.json()["movie_count"] == 1
    assert list_movies_response.status_code == 200
    assert list_movies_response.json()["items"][0]["movie_number"] == movie.movie_number
    assert "playlist_item_updated_at" in list_movies_response.json()["items"][0]
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "稍后再看"
    assert remove_response.status_code == 204
    assert delete_response.status_code == 204
    assert Playlist.get_or_none(Playlist.id == playlist_id) is None


def test_playlist_api_rejects_reserved_name_and_system_mutations(client, account_user):
    token = _login(client, username=account_user.username)
    system_playlist = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
        description="系统自动维护的最近播放影片列表",
    )
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")

    reserved_name_response = client.post(
        "/playlists",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "最近播放", "description": ""},
    )
    update_response = client.patch(
        f"/playlists/{system_playlist.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "新名字"},
    )
    delete_response = client.delete(
        f"/playlists/{system_playlist.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    add_response = client.put(
        f"/playlists/{system_playlist.id}/movies/{movie.movie_number}",
        headers={"Authorization": f"Bearer {token}"},
    )
    remove_response = client.delete(
        f"/playlists/{system_playlist.id}/movies/{movie.movie_number}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert reserved_name_response.status_code == 409
    assert reserved_name_response.json()["error"]["code"] == "playlist_reserved_name"
    assert update_response.status_code == 409
    assert update_response.json()["error"]["code"] == "playlist_managed_by_system"
    assert delete_response.status_code == 409
    assert delete_response.json()["error"]["code"] == "playlist_managed_by_system"
    assert add_response.status_code == 409
    assert add_response.json()["error"]["code"] == "playlist_managed_by_system"
    assert remove_response.status_code == 409
    assert remove_response.json()["error"]["code"] == "playlist_managed_by_system"


def test_playlist_api_returns_expected_not_found_errors(client, account_user):
    token = _login(client, username=account_user.username)
    playlist = Playlist.create(name="我的收藏", description="Favorite")

    get_missing_response = client.get(
        "/playlists/999",
        headers={"Authorization": f"Bearer {token}"},
    )
    add_missing_movie_response = client.put(
        f"/playlists/{playlist.id}/movies/ABC-404",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert get_missing_response.status_code == 404
    assert get_missing_response.json()["error"]["code"] == "playlist_not_found"
    assert add_missing_movie_response.status_code == 404
    assert add_missing_movie_response.json()["error"]["code"] == "movie_not_found"


def test_list_playlists_includes_system_first(client, account_user):
    token = _login(client, username=account_user.username)
    system_playlist = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
        description="系统自动维护的最近播放影片列表",
    )
    custom_playlist = Playlist.create(name="我的收藏", description="Favorite")
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    PlaylistMovie.create(playlist=system_playlist, movie=movie)
    PlaylistMovie.create(playlist=custom_playlist, movie=movie)

    response = client.get(
        "/playlists",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [system_playlist.id, custom_playlist.id]
    assert response.json()[0]["movie_count"] == 1
