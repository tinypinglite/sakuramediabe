from src.model import Media, Movie, ResourceTaskState


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


def test_list_resource_task_state_definitions_returns_counts(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-101", "Movie101")
    ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=movie.id,
        state="failed",
    )

    response = client.get(
        "/system/resource-task-states/definitions",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    movie_desc_sync = next(item for item in payload if item["task_key"] == "movie_desc_sync")
    assert movie_desc_sync["display_name"] == "影片描述回填"
    assert movie_desc_sync["state_counts"]["failed"] == 1


def test_list_resource_task_states_supports_paging_filter_and_search(client, account_user):
    token = _login(client, username=account_user.username)
    movie_a = _create_movie("ABP-201", "Movie201", title="第一部")
    movie_b = _create_movie("ABP-202", "Movie202", title="第二部")
    ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=movie_a.id,
        state="failed",
        attempt_count=2,
        last_error="boom",
    )
    ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=movie_b.id,
        state="succeeded",
        attempt_count=1,
    )

    response = client.get(
        "/system/resource-task-states?task_key=movie_desc_sync&state=failed&search=ABP-201&page=1&page_size=20",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["resource"]["movie_number"] == "ABP-201"
    assert payload["items"][0]["resource"]["title"] == "第一部"
    assert payload["items"][0]["last_error"] == "boom"


def test_list_media_task_records_returns_media_resource_summary(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-401", "Movie401", title="媒体任务影片")
    media = Media.create(movie=movie, path="/library/main/abp-401.mp4", valid=True)
    ResourceTaskState.create(
        task_key="media_thumbnail_generation",
        resource_type="media",
        resource_id=media.id,
        state="failed",
        attempt_count=1,
        last_error="thumbnail_generation_empty",
    )

    response = client.get(
        "/system/resource-task-states?task_key=media_thumbnail_generation&search=abp-401.mp4",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["resource"]["path"] == "/library/main/abp-401.mp4"
    assert payload["items"][0]["resource"]["movie_number"] == "ABP-401"
