from src.model import (
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Actor,
    Movie,
    MovieActor,
    MovieTag,
    Playlist,
    PlaylistMovie,
    Tag,
)


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_signal_movies() -> None:
    playlist = Playlist.create(kind=PLAYLIST_KIND_RECENTLY_PLAYED, name="最近播放")
    actress = Actor.create(
        name="测试女优",
        gender=1,
        cup="C",
        is_subscribed=True,
    )
    tag = Tag.create(name="剧情")
    for movie_number in ("API-001", "API-002"):
        movie = Movie.create(
            movie_number=movie_number,
            title=movie_number,
        )
        PlaylistMovie.create(playlist=playlist, movie=movie)
        MovieActor.create(movie=movie, actor=actress)
        MovieTag.create(movie=movie, tag=tag)


def test_taste_profile_requires_authentication(client):
    assert client.get("/taste-profile").status_code == 401


def test_taste_profile_returns_ranked_actresses_and_tags(client, account_user):
    _create_signal_movies()

    response = client.get("/taste-profile", headers=_login(client, account_user.username))

    assert response.status_code == 200
    payload = response.json()
    assert payload["generated_at"]
    assert payload["sample_movie_count"] == 2
    assert payload["actresses"][0]["actor"]["name"] == "测试女优"
    assert payload["actresses"][0]["actor"]["is_subscribed"] is True
    assert payload["actresses"][0]["matched_movie_count"] == 2
    assert payload["actresses"][0]["score"] == 2.0
    assert payload["unsubscribed_actresses"] == []
    assert payload["tags"][0]["name"] == "剧情"
    assert payload["tags"][0]["matched_movie_count"] == 2
    assert payload["tags"][0]["score"] == 2.0
