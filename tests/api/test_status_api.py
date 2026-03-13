from src.model import Actor, Media, MediaLibrary, Movie


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


def test_status_endpoint_requires_authentication(client):
    response = client.get("/status")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_status_endpoint_returns_zero_summary_when_library_is_empty(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get("/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "actors": {
            "female_total": 0,
            "female_subscribed": 0,
        },
        "movies": {
            "total": 0,
            "subscribed": 0,
            "playable": 0,
        },
        "media_files": {
            "total": 0,
            "total_size_bytes": 0,
        },
        "media_libraries": {
            "total": 0,
        },
    }


def test_status_endpoint_returns_aggregated_summary(client, account_user):
    token = _login(client, username=account_user.username)

    Actor.create(name="actor-1", javdb_id="ActorA1", gender=1, is_subscribed=True)
    Actor.create(name="actor-2", javdb_id="ActorA2", gender=1, is_subscribed=False)
    Actor.create(name="actor-3", javdb_id="ActorA3", gender=2, is_subscribed=True)
    Actor.create(name="actor-4", javdb_id="ActorA4", gender=0, is_subscribed=True)

    movie_a = _create_movie("ABC-001", "MovieA1", is_subscribed=True)
    movie_b = _create_movie("ABC-002", "MovieA2", is_subscribed=False)
    movie_c = _create_movie("ABC-003", "MovieA3", is_subscribed=True)

    library_main = MediaLibrary.create(name="Main", root_path="/library/main")
    library_archive = MediaLibrary.create(name="Archive", root_path="/library/archive")

    Media.create(
        movie=movie_a,
        path="/library/main/abc-001-main.mp4",
        library=library_main,
        valid=True,
        file_size_bytes=100,
    )
    Media.create(
        movie=movie_a,
        path="/library/main/abc-001-backup.mp4",
        library=library_main,
        valid=True,
        file_size_bytes=200,
    )
    Media.create(
        movie=movie_b,
        path="/library/main/abc-002.mp4",
        library=library_main,
        valid=False,
        file_size_bytes=300,
    )
    Media.create(
        movie=movie_c,
        path="/library/archive/abc-003.mp4",
        library=library_archive,
        valid=True,
        file_size_bytes=400,
    )

    response = client.get("/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "actors": {
            "female_total": 2,
            "female_subscribed": 1,
        },
        "movies": {
            "total": 3,
            "subscribed": 2,
            "playable": 2,
        },
        "media_files": {
            "total": 4,
            "total_size_bytes": 1000,
        },
        "media_libraries": {
            "total": 2,
        },
    }
