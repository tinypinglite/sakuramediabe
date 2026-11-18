from src.model import Actor, Movie, MovieActor


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_local_actor_create_and_identity_edit(client, account_user):
    headers = _auth_headers(client, account_user)

    created = client.post(
        "/actors",
        headers=headers,
        json={"name": " 本地女优 ", "alias_name": "别名A / 别名B"},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["name"] == "本地女优"
    assert payload["alias_name"] == "本地女优 / 别名A / 别名B"
    assert payload["display_name"] == "本地女优"
    assert payload["gender"] == 1
    assert payload["manual_fields"] == ["gender"]

    duplicate = client.post("/actors", headers=headers, json={"name": "本地女优"})
    assert duplicate.status_code == 201

    renamed = client.patch(
        f"/actors/{payload['id']}",
        headers=headers,
        json={"name": "新名字"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "新名字"
    assert renamed.json()["alias_name"] == "新名字 / 本地女优 / 别名A / 别名B"

    aliased = client.patch(
        f"/actors/{payload['id']}",
        headers=headers,
        json={"alias_name": "新别名"},
    )
    assert aliased.status_code == 200
    assert aliased.json()["alias_name"] == "新名字 / 新别名"


def test_movie_actor_add_remove_and_manual_ownership(client, account_user):
    movie = Movie.create(movie_number="API-CAST-1", title="影片")
    first_actor = Actor.create(name="女优一")
    second_actor = Actor.create(name="女优二")
    headers = _auth_headers(client, account_user)

    for actor in (first_actor, second_actor):
        assert (
            client.put(
                f"/movies/{movie.movie_number}/actors/{actor.id}",
                headers=headers,
            ).status_code
            == 204
        )

    detail = client.get(f"/movies/{movie.movie_number}", headers=headers)
    assert detail.status_code == 200
    assert {item["id"] for item in detail.json()["actors"]} == {
        first_actor.id,
        second_actor.id,
    }
    assert detail.json()["is_actors_manually_managed"] is True

    assert (
        client.delete(
            f"/movies/{movie.movie_number}/actors/{first_actor.id}",
            headers=headers,
        ).status_code
        == 204
    )
    assert (
        client.delete(
            f"/movies/{movie.movie_number}/actors/{first_actor.id}",
            headers=headers,
        ).status_code
        == 204
    )
    detail = client.get(f"/movies/{movie.movie_number}", headers=headers)
    assert [item["id"] for item in detail.json()["actors"]] == [second_actor.id]

    reset = client.post(
        f"/movies/{movie.movie_number}/actors/reset", headers=headers
    )
    assert reset.status_code == 204
    assert (
        client.get(f"/movies/{movie.movie_number}", headers=headers).json()[
            "is_actors_manually_managed"
        ]
        is False
    )


def test_actor_delete_requires_no_references(client, account_user):
    movie = Movie.create(movie_number="API-CAST-1", title="影片")
    actor = Actor.create(name="女优")
    headers = _auth_headers(client, account_user)

    client.put(
        f"/movies/{movie.movie_number}/actors/{actor.id}", headers=headers
    )
    in_use = client.delete(f"/actors/{actor.id}", headers=headers)
    assert in_use.status_code == 409
    assert in_use.json()["error"]["code"] == "actor_in_use"

    MovieActor.delete().where(MovieActor.actor == actor.id).execute()
    deleted = client.delete(f"/actors/{actor.id}", headers=headers)
    assert deleted.status_code == 204
    assert client.get(f"/actors/{actor.id}", headers=headers).status_code == 404


def test_actor_can_be_subscribed_as_favorite(client, account_user):
    actor = Actor.create(name="女优")
    headers = _auth_headers(client, account_user)

    assert (
        client.put(f"/actors/{actor.id}/subscription", headers=headers).status_code
        == 204
    )
    fresh = Actor.get_by_id(actor.id)
    assert fresh.is_subscribed is True
    assert fresh.subscribed_at is not None

    assert (
        client.delete(f"/actors/{actor.id}/subscription", headers=headers).status_code
        == 204
    )
    fresh = Actor.get_by_id(actor.id)
    assert fresh.is_subscribed is False
    assert fresh.subscribed_at is None


def test_actor_merge_carries_subscription(client, account_user):
    source = Actor.create(name="来源", is_subscribed=True)
    target = Actor.create(name="保留")
    headers = _auth_headers(client, account_user)

    merged = client.post(
        f"/actors/{target.id}/merge",
        headers=headers,
        json={"source_actor_ids": [source.id]},
    )
    assert merged.status_code == 200
    assert merged.json()["id"] == target.id
    refreshed = Actor.get_by_id(target.id)
    assert refreshed.is_subscribed is True
    assert refreshed.subscribed_at is not None
