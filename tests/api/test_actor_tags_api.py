from src.model import Actor, Movie, MovieActor, MovieTag, Tag


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_actor_tags_return_movie_counts_scoped_to_actor(client, account_user):
    actor = Actor.create(name="演员", gender=1)
    other_actor = Actor.create(name="其他演员", gender=1)
    first = Movie.create(movie_number="TAGS-001", title="第一作")
    second = Movie.create(movie_number="TAGS-002", title="第二作")
    other = Movie.create(movie_number="TAGS-003", title="其他作品")
    shared = Tag.create(name="shared")
    unique = Tag.create(name="unique")
    other_only = Tag.create(name="other-only")

    MovieActor.create(movie=first, actor=actor)
    MovieActor.create(movie=second, actor=actor)
    MovieActor.create(movie=other, actor=other_actor)
    for movie in (first, second, other):
        MovieTag.create(movie=movie, tag=shared)
    MovieTag.create(movie=first, tag=unique)
    MovieTag.create(movie=other, tag=unique)
    MovieTag.create(movie=other, tag=other_only)

    response = client.get(
        f"/actors/{actor.id}/tags", headers=_auth_headers(client, account_user)
    )

    assert response.status_code == 200
    assert response.json() == [
        {"tag_id": shared.id, "name": "shared", "movie_count": 2},
        {"tag_id": unique.id, "name": "unique", "movie_count": 1},
    ]
