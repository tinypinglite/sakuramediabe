from datetime import timedelta

from src.common.runtime_time import utc_now_for_db
from src.model import (
    VIEW_HISTORY_MAX_ENTRIES,
    Actor,
    Movie,
    MovieActor,
    ViewHistory,
)


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth_headers(client, account_user) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, account_user.username)}"}


def _create_movie(movie_number: str) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=f"title-{movie_number}",
    )


def _record(client, headers, entity_type: str, entity_id: int):
    return client.post(
        "/view-history",
        headers=headers,
        json={"entity_type": entity_type, "entity_id": entity_id},
    )


def test_view_history_records_and_lists_movies_and_actors(client, account_user):
    headers = _auth_headers(client, account_user)
    movie = _create_movie("VH-001")
    actor = Actor.create(javdb_id="actor-vh-1", name="view actress", gender=0)
    MovieActor.create(movie=movie, actor=actor)

    assert _record(client, headers, "movie", movie.id).status_code == 204
    assert _record(client, headers, "actor", actor.id).status_code == 204

    response = client.get("/view-history", headers=headers)
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["entity_type"] for item in items] == ["actor", "movie"]
    actor_resource = items[0]["actor"]
    assert actor_resource["id"] == actor.id
    assert actor_resource["display_name"] == "view actress"
    assert actor_resource["movie_count"] == 1
    assert actor_resource["is_subscribed"] is False
    assert items[0]["movie"] is None
    movie_resource = items[1]["movie"]
    assert movie_resource["id"] == movie.id
    assert movie_resource["movie_number"] == "VH-001"
    assert movie_resource["title"] == "title-VH-001"
    assert movie_resource["can_play"] is False
    assert movie_resource["media_items"] == []
    assert items[1]["actor"] is None

    assert client.get("/view-history?limit=0", headers=headers).status_code == 422


def test_view_history_dedupes_and_refreshes_last_viewed_at(client, account_user):
    headers = _auth_headers(client, account_user)
    movie = _create_movie("VH-002")
    actor = Actor.create(javdb_id="actor-vh-2", name="second actress", gender=0)

    assert _record(client, headers, "movie", movie.id).status_code == 204
    assert _record(client, headers, "actor", actor.id).status_code == 204
    assert _record(client, headers, "movie", movie.id).status_code == 204

    assert ViewHistory.select().count() == 2
    items = client.get("/view-history", headers=headers).json()["items"]
    assert [item["entity_type"] for item in items] == ["movie", "actor"]


def test_view_history_prunes_to_max_entries(client, account_user):
    headers = _auth_headers(client, account_user)
    now = utc_now_for_db()
    ViewHistory.insert_many(
        [
            {
                "entity_type": "movie",
                "entity_id": 100000 + offset,
                "viewed_at": now - timedelta(seconds=offset + 1),
            }
            for offset in range(VIEW_HISTORY_MAX_ENTRIES + 5)
        ]
    ).execute()

    movie = _create_movie("VH-PRUNE")
    assert _record(client, headers, "movie", movie.id).status_code == 204

    assert ViewHistory.select().count() == VIEW_HISTORY_MAX_ENTRIES
    assert (
        ViewHistory.select()
        .where(ViewHistory.entity_id == movie.id)
        .exists()
    )


def test_view_history_filters_deleted_entities(client, account_user):
    headers = _auth_headers(client, account_user)
    movie = _create_movie("VH-GONE")
    assert _record(client, headers, "movie", movie.id).status_code == 204

    movie.delete_instance()

    response = client.get("/view-history", headers=headers)
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_view_history_resolves_merged_actor_to_canonical(client, account_user):
    headers = _auth_headers(client, account_user)
    target = Actor.create(javdb_id="actor-vh-merge-target", name="canonical", gender=0)
    source = Actor.create(javdb_id="actor-vh-merge-source", name="merged", gender=0)

    assert _record(client, headers, "actor", source.id).status_code == 204
    merged = client.post(
        f"/actors/{target.id}/merge",
        headers=headers,
        json={"source_actor_ids": [source.id]},
    )
    assert merged.status_code == 200

    items = client.get("/view-history", headers=headers).json()["items"]
    assert len(items) == 1
    assert items[0]["actor"]["id"] == target.id
    assert items[0]["actor"]["display_name"] == "canonical"


def test_view_history_dedupes_merged_actor_entries(client, account_user):
    headers = _auth_headers(client, account_user)
    target = Actor.create(javdb_id="actor-vh-dedupe-target", name="canonical", gender=0)
    source = Actor.create(javdb_id="actor-vh-dedupe-source", name="merged", gender=0)

    assert _record(client, headers, "actor", source.id).status_code == 204
    assert _record(client, headers, "actor", target.id).status_code == 204
    merged = client.post(
        f"/actors/{target.id}/merge",
        headers=headers,
        json={"source_actor_ids": [source.id]},
    )
    assert merged.status_code == 200

    items = client.get("/view-history", headers=headers).json()["items"]
    assert [item["actor"]["id"] for item in items] == [target.id]


def test_view_history_rejects_unknown_entity_type(client, account_user):
    headers = _auth_headers(client, account_user)
    response = client.post(
        "/view-history",
        headers=headers,
        json={"entity_type": "video", "entity_id": 1},
    )
    assert response.status_code == 422
