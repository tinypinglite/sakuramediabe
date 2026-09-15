from src.model import Media, MediaLibrary, Movie


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_media_list_exposes_and_filters_thumbnail_terminal_state(client, account_user):
    library = MediaLibrary.create(
        name="thumbnail-api-library",
        provider_key="test",
        provider_config={},
    )
    terminal_movie = Movie.create(
        movie_number="THAPI-001", javdb_id="thapi-1", title="terminal"
    )
    pending_movie = Movie.create(
        movie_number="THAPI-002", javdb_id="thapi-2", title="pending"
    )
    terminal_media = Media.create(
        movie=terminal_movie,
        library=library,
        file_name="terminal.mp4",
        thumbnail_generation_state=Media.THUMBNAIL_STATE_TERMINAL,
        thumbnail_last_error_code="video_file_missing",
    )
    Media.create(
        movie=pending_movie,
        library=library,
        file_name="pending.mp4",
    )

    response = client.get(
        "/media",
        params={"thumbnail_generation_state": "terminal"},
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [terminal_media.id]
    assert body["items"][0]["thumbnail_generation_state"] == "terminal"
    assert body["items"][0]["thumbnail_last_error_code"] == "video_file_missing"


def test_reset_media_thumbnail_terminal_state(client, account_user):
    library = MediaLibrary.create(
        name="thumbnail-reset-library",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="THRESET-001", javdb_id="threset-1", title="reset"
    )
    media = Media.create(
        movie=movie,
        library=library,
        file_name="reset.mp4",
        thumbnail_generation_state=Media.THUMBNAIL_STATE_TERMINAL,
        thumbnail_attempt_count=2,
        thumbnail_deferred_count=3,
        thumbnail_next_retry_at="2026-03-12 10:00:00",
        thumbnail_last_error_code="thumbnail_generation_empty",
        thumbnail_last_error="no artifacts",
        thumbnail_terminal_at="2026-03-12 10:00:00",
    )

    response = client.post(
        "/media/thumbnail-generation/reset",
        json={"media_ids": [media.id]},
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    assert response.json() == {"reset_count": 1}
    reset_media = Media.get_by_id(media.id)
    assert reset_media.thumbnail_generation_state == Media.THUMBNAIL_STATE_PENDING
    assert reset_media.thumbnail_attempt_count == 0
    assert reset_media.thumbnail_deferred_count == 0
    assert reset_media.thumbnail_next_retry_at is None
    assert reset_media.thumbnail_last_error_code is None
    assert reset_media.thumbnail_last_error is None
    assert reset_media.thumbnail_terminal_at is None
