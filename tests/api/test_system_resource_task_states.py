from dataclasses import replace

from src.model import Media, Movie, ResourceTaskState
from src.service.system.resource_task_state_service import ResourceTaskStateService


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


def test_reset_resource_task_state_only_allows_failed_records(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-301", "Movie301")
    failed_record = ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=movie.id,
        state="failed",
        attempt_count=3,
        last_error="boom",
    )

    reset_response = client.post(
        f"/system/resource-task-states/movie_desc_sync/{movie.id}/reset",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert reset_response.status_code == 200
    reset_payload = reset_response.json()
    assert reset_payload["state"] == "pending"
    assert reset_payload["attempt_count"] == 0
    assert reset_payload["last_error"] is None

    failed_record.state = "running"
    failed_record.save(only=[ResourceTaskState.state])
    rejected_response = client.post(
        f"/system/resource-task-states/movie_desc_sync/{movie.id}/reset",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert rejected_response.status_code == 422
    assert rejected_response.json()["error"]["code"] == "resource_task_state_reset_forbidden"


def test_reset_resource_task_state_rejects_task_without_allow_reset(
    client, account_user, monkeypatch
):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-302", "Movie302")
    definition = ResourceTaskStateService.get_definition("movie_desc_sync")
    monkeypatch.setitem(
        ResourceTaskStateService.TASK_REGISTRY,
        "movie_desc_sync",
        replace(definition, allow_reset=False),
    )
    ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=movie.id,
        state="failed",
        attempt_count=1,
        last_error="boom",
    )

    response = client.post(
        f"/system/resource-task-states/movie_desc_sync/{movie.id}/reset",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "resource_task_state_reset_forbidden"


def test_reset_thumbnail_task_clears_terminal_flag_for_media_list(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-303", "Movie303", title="缩略图重置")
    media = Media.create(movie=movie, path="/library/main/abp-303.mp4", valid=True)
    ResourceTaskState.create(
        task_key="media_thumbnail_generation",
        resource_type="media",
        resource_id=media.id,
        state="failed",
        attempt_count=2,
        last_error="thumbnail_generation_empty",
        extra={"terminal": True},
    )

    before_response = client.get(
        "/media?page=1&page_size=20",
        headers={"Authorization": f"Bearer {token}"},
    )
    reset_response = client.post(
        f"/system/resource-task-states/media_thumbnail_generation/{media.id}/reset",
        headers={"Authorization": f"Bearer {token}"},
    )
    after_response = client.get(
        "/media?page=1&page_size=20",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert before_response.status_code == 200
    assert before_response.json()["items"][0]["need_thumbnail_generation"] is False
    assert reset_response.status_code == 200
    assert after_response.status_code == 200
    assert after_response.json()["items"][0]["need_thumbnail_generation"] is True
    assert after_response.json()["items"][0]["thumbnail_retry_count"] == 0
    assert after_response.json()["items"][0]["thumbnail_last_error"] is None


def test_reset_movie_desc_task_clears_terminal_flag(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-304", "Movie304", title="影片描述重置")
    ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=movie.id,
        state="failed",
        attempt_count=2,
        last_error="DMM 未找到对应番号: ABP-304",
        extra={"terminal": True, "source": "dmm"},
    )

    response = client.post(
        f"/system/resource-task-states/movie_desc_sync/{movie.id}/reset",
        headers={"Authorization": f"Bearer {token}"},
    )
    refreshed = ResourceTaskState.get(
        ResourceTaskState.task_key == "movie_desc_sync",
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == movie.id,
    )

    assert response.status_code == 200
    assert refreshed.state == "pending"
    assert refreshed.extra == {"source": "dmm"}


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
