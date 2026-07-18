from src.model import Media, MediaLibrary, Movie, ResourceTaskState


RESET_PATH = "/system/resource-task-states/media_thumbnail_generation/reset"
TASK_KEY = "media_thumbnail_generation"


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _create_media(movie_number: str, *, valid: bool = True) -> Media:
    movie = Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=f"title-{movie_number}",
    )
    library, _ = MediaLibrary.get_or_create(
        name="test-library",
        defaults={"backend": "local", "backend_config": {"root_path": "/library"}},
    )
    return Media.create(
        movie=movie,
        library=library,
        path=f"/library/{movie_number}.mp4",
        valid=valid,
    )


def _create_task_state(media: Media, *, state: str = "failed", terminal: bool = True):
    return ResourceTaskState.create(
        task_key=TASK_KEY,
        resource_type="media",
        resource_id=media.id,
        state=state,
        attempt_count=2,
        last_error="thumbnail_generation_empty",
        last_trigger_type="scheduled",
        last_task_run_id=10,
        extra={"terminal": terminal, "source": "test"},
    )


def test_batch_reset_requeues_failed_media_thumbnail_tasks(client, account_user):
    token = _login(client, account_user.username)
    first_media = _create_media("ABC-001")
    second_media = _create_media("ABC-002")
    _create_task_state(first_media)
    _create_task_state(second_media)

    response = client.post(
        RESET_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={"resource_ids": [first_media.id, second_media.id]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "task_key": TASK_KEY,
        "state": "pending",
        "reset_count": 2,
        "resource_ids": [first_media.id, second_media.id],
    }
    for media in (first_media, second_media):
        record = ResourceTaskState.get(
            ResourceTaskState.task_key == TASK_KEY,
            ResourceTaskState.resource_id == media.id,
        )
        assert record.state == "pending"
        assert record.attempt_count == 0
        assert record.last_error is None
        assert record.last_task_run_id is None
        assert record.last_trigger_type == "manual"
        assert record.extra == {"source": "test"}


def test_batch_reset_is_atomic_when_one_media_is_not_failed(client, account_user):
    token = _login(client, account_user.username)
    failed_media = _create_media("ABC-003")
    pending_media = _create_media("ABC-004")
    _create_task_state(failed_media)
    _create_task_state(pending_media, state="pending", terminal=False)

    response = client.post(
        RESET_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={"resource_ids": [failed_media.id, pending_media.id]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_thumbnail_task_reset_forbidden"
    failed_record = ResourceTaskState.get(
        ResourceTaskState.task_key == TASK_KEY,
        ResourceTaskState.resource_id == failed_media.id,
    )
    assert failed_record.state == "failed"
    assert failed_record.attempt_count == 2
