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
        backend="local",
        backend_config={"root_path": "/library"},
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
        path="/library/terminal.mp4",
        content_fingerprint="terminal-fingerprint",
        thumbnail_generation_state=Media.THUMBNAIL_STATE_TERMINAL,
        thumbnail_last_error_code="video_file_missing",
    )
    Media.create(
        movie=pending_movie,
        library=library,
        path="/library/pending.mp4",
        content_fingerprint="pending-fingerprint",
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
