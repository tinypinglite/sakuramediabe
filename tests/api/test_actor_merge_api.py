from src.model import Actor, Movie, MovieActor


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_merge_actor_api_merges_and_resolves_old_identity(client, account_user):
    target = Actor.create(javdb_id="api-merge-target", name="新名", gender=1)
    source = Actor.create(javdb_id="api-merge-source", name="旧名", gender=1)
    movie = Movie.create(
        movie_number="API-MERGE-1",
        javdb_id="javdb-API-MERGE-1",
        title="API-MERGE-1",
    )
    MovieActor.create(movie=movie, actor=source)
    headers = _auth_headers(client, account_user)

    response = client.post(
        f"/actors/{target.id}/merge",
        headers=headers,
        json={"source_actor_ids": [source.id]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == target.id
    assert payload["movie_count"] == 1

    listing = client.get("/actors", headers=headers, params={"query": "旧名"})
    assert [item["id"] for item in listing.json()["items"]] == [target.id]

    detail = client.get(f"/actors/{source.id}", headers=headers)
    assert detail.json()["id"] == target.id

    movies = client.get("/movies", headers=headers, params={"actor_id": source.id})
    assert [item["id"] for item in movies.json()["items"]] == [movie.id]

    patched = client.patch(
        f"/actors/{source.id}", headers=headers, json={"height_cm": 160}
    )
    assert patched.status_code == 200
    assert patched.json()["id"] == target.id
    assert patched.json()["height_cm"] == 160

    subscribed = client.put(f"/actors/{source.id}/subscription", headers=headers)
    assert subscribed.status_code == 204
    assert Actor.get_by_id(target.id).is_subscribed is True

    unsubscribed = client.delete(f"/actors/{source.id}/subscription", headers=headers)
    assert unsubscribed.status_code == 204
    assert Actor.get_by_id(target.id).is_subscribed is False


def test_merge_actor_api_validation(client, account_user):
    actor = Actor.create(javdb_id="api-merge-actor", name="演员")
    headers = _auth_headers(client, account_user)

    self_merge = client.post(
        f"/actors/{actor.id}/merge",
        headers=headers,
        json={"source_actor_ids": [actor.id]},
    )
    assert self_merge.status_code == 422
    assert self_merge.json()["error"]["code"] == "invalid_actor_merge"

    missing = client.post(
        f"/actors/{actor.id}/merge",
        headers=headers,
        json={"source_actor_ids": [999999]},
    )
    assert missing.status_code == 404

    empty = client.post(
        f"/actors/{actor.id}/merge",
        headers=headers,
        json={"source_actor_ids": []},
    )
    assert empty.status_code == 422
