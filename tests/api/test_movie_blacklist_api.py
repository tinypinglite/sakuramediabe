from src.model import Movie

BLACKLIST_PATH = "/movies/blacklist"


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _create_movie(movie_number: str, *, is_subscribed: bool = False) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=f"title-{movie_number}",
        is_subscribed=is_subscribed,
    )


def test_plugin_blacklist_hides_movie_and_manual_unblock_wins(client, account_user):
    from src.plugins.context import MovieApi

    movie = _create_movie("FILTER-001")
    api = MovieApi("filter_plugin")
    headers = {"Authorization": f"Bearer {_login(client, account_user.username)}"}
    assert api.patch(movie.id, {"is_blacklisted": True}, expected_revision=0)
    response = client.get("/movies", headers=headers)
    assert response.status_code == 200
    assert response.json()["items"] == []

    response = client.request(
        "DELETE", BLACKLIST_PATH, headers=headers,
        json={"movie_numbers": [movie.movie_number]},
    )
    assert response.status_code == 204
    snapshot = api.get(movie.id)
    assert snapshot.owners["is_blacklisted"] == "host:manual"
    assert not api.patch(movie.id, {"is_blacklisted": True}, snapshot.revision)
    response = client.get("/movies", headers=headers)
    assert response.status_code == 200
    assert [item["movie_number"] for item in response.json()["items"]] == [movie.movie_number]


def test_batch_blacklist_hides_movies_and_can_be_reversed(client, account_user):
    token = _login(client, account_user.username)
    first = _create_movie("BL-001")
    second = _create_movie("BL-002")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        BLACKLIST_PATH,
        headers=headers,
        json={"movie_numbers": [first.movie_number, second.movie_number]},
    )

    assert response.status_code == 204
    assert Movie.get_by_id(first.id).is_blacklisted is True
    assert Movie.get_by_id(second.id).is_blacklisted is True
    assert Movie.get_by_id(first.id).field_owners == {"is_blacklisted": "host:manual"}
    assert Movie.get_by_id(first.id).mutation_revision == 1

    visible_response = client.get("/movies", headers=headers)
    assert visible_response.status_code == 200
    assert visible_response.json()["items"] == []

    blacklisted_response = client.get("/movies?blacklisted=true", headers=headers)
    assert blacklisted_response.status_code == 200
    assert {item["movie_number"] for item in blacklisted_response.json()["items"]} == {
        "BL-001",
        "BL-002",
    }

    response = client.request(
        "DELETE",
        BLACKLIST_PATH,
        headers=headers,
        json={"movie_numbers": [first.movie_number, second.movie_number]},
    )

    assert response.status_code == 204
    assert Movie.get_by_id(first.id).is_blacklisted is False
    assert Movie.get_by_id(second.id).is_blacklisted is False
    assert Movie.get_by_id(first.id).field_owners == {"is_blacklisted": "host:manual"}
    assert Movie.get_by_id(first.id).mutation_revision == 2


def test_batch_blacklist_rejects_subscribed_movies_without_partial_update(client, account_user):
    token = _login(client, account_user.username)
    normal = _create_movie("BL-010")
    subscribed = _create_movie("BL-011", is_subscribed=True)

    response = client.put(
        BLACKLIST_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={"movie_numbers": [normal.movie_number, subscribed.movie_number]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "movie_is_subscribed"
    assert Movie.get_by_id(normal.id).is_blacklisted is False
    assert Movie.get_by_id(subscribed.id).is_blacklisted is False
    assert Movie.get_by_id(normal.id).field_owners == {}
    assert Movie.get_by_id(normal.id).mutation_revision == 0


def test_blacklisted_movie_cannot_be_subscribed(client, account_user):
    token = _login(client, account_user.username)
    movie = _create_movie("BL-020")
    headers = {"Authorization": f"Bearer {token}"}
    client.put(BLACKLIST_PATH, headers=headers, json={"movie_numbers": [movie.movie_number]})

    response = client.put(f"/movies/{movie.movie_number}/subscription", headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "movie_is_blacklisted"


def test_batch_blacklist_rejects_more_than_1000_numbers(client, account_user):
    token = _login(client, account_user.username)

    response = client.put(
        BLACKLIST_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={"movie_numbers": [f"BL-{index}" for index in range(1001)]},
    )

    assert response.status_code == 422
