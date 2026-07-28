from src.model import BackgroundTaskRun, Media, MediaLibrary, Movie, ResourceTaskState


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
    # last_task_run_id 已外键化（Wave 0），必须指向真实的 task_run 行。
    task_run = BackgroundTaskRun.create(
        task_key=TASK_KEY,
        task_name="媒体缩略图生成",
        trigger_type="scheduled",
        state="completed",
    )
    return ResourceTaskState.create(
        task_key=TASK_KEY,
        resource_type="media",
        resource_id=media.id,
        state=state,
        attempt_count=2,
        last_error="thumbnail_generation_empty",
        last_trigger_type="scheduled",
        last_task_run_id=task_run.id,
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
        "skipped_count": 0,
        "skipped": [],
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


def test_batch_reset_skips_unqualified_media_and_requeues_the_rest(client, account_user):
    token = _login(client, account_user.username)
    failed_media = _create_media("ABC-003")
    pending_media = _create_media("ABC-004")
    invalid_media = _create_media("ABC-005", valid=False)
    deleted_media = _create_media("ABC-006")
    _create_task_state(failed_media)
    _create_task_state(pending_media, state="pending", terminal=False)
    _create_task_state(invalid_media)
    _create_task_state(deleted_media)
    # 只删媒体、留下任务记录，复现巡检/外部删除后残留的孤儿记录。
    deleted_media_id = deleted_media.id
    Media.delete().where(Media.id == deleted_media_id).execute()
    missing_state_resource_id = deleted_media_id + 10_000

    response = client.post(
        RESET_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "resource_ids": [
                failed_media.id,
                pending_media.id,
                invalid_media.id,
                deleted_media_id,
                missing_state_resource_id,
            ]
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "task_key": TASK_KEY,
        "state": "pending",
        "reset_count": 1,
        "resource_ids": [failed_media.id],
        "skipped_count": 4,
        "skipped": [
            {"resource_id": pending_media.id, "reason": "not_failed"},
            {"resource_id": invalid_media.id, "reason": "media_invalid"},
            {"resource_id": deleted_media_id, "reason": "media_not_found"},
            {"resource_id": missing_state_resource_id, "reason": "task_state_not_found"},
        ],
    }

    reset_record = ResourceTaskState.get(
        ResourceTaskState.task_key == TASK_KEY,
        ResourceTaskState.resource_id == failed_media.id,
    )
    assert reset_record.state == "pending"
    assert reset_record.attempt_count == 0
    for skipped_media in (invalid_media, deleted_media):
        untouched = ResourceTaskState.get(
            ResourceTaskState.task_key == TASK_KEY,
            ResourceTaskState.resource_id == skipped_media.id,
        )
        assert untouched.state == "failed"
        assert untouched.attempt_count == 2


def test_batch_reset_returns_all_skipped_when_nothing_is_eligible(client, account_user):
    token = _login(client, account_user.username)
    invalid_media = _create_media("ABC-007", valid=False)
    _create_task_state(invalid_media)

    response = client.post(
        RESET_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={"resource_ids": [invalid_media.id]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reset_count"] == 0
    assert body["resource_ids"] == []
    assert body["skipped"] == [{"resource_id": invalid_media.id, "reason": "media_invalid"}]
