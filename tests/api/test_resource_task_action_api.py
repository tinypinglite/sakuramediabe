"""统一资源任务操作端点（POST /system/resource-task-actions）API 护栏。

资源级操作的唯一入口：旧的缩略图专用 reset 端点与订阅 search-resets 端点已删除，
本文件覆盖它们的语义在统一协议下的对等物（媒体合格性钩子 / 批量按状态圈定）。
"""

from datetime import timedelta

from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun, Media, MediaLibrary, Movie, ResourceTaskState

ACTION_PATH = "/system/resource-task-actions"
THUMBNAIL_TASK_KEY = "media_thumbnail_generation"


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

def _create_task_state(media: Media, *, state: str = "failed_terminal"):
    # last_task_run_id 已外键化（Wave 0），必须指向真实的 task_run 行。
    task_run = BackgroundTaskRun.create(
        task_key=THUMBNAIL_TASK_KEY,
        task_name="媒体缩略图生成",
        trigger_type="scheduled",
        state="completed",
    )
    return ResourceTaskState.create(
        task_key=THUMBNAIL_TASK_KEY,
        resource_type="media",
        resource_id=media.id,
        state=state,
        attempt_count=2,
        last_error="thumbnail_generation_empty",
        error_code="thumbnail_generation_empty",
        last_trigger_type="scheduled",
        last_task_run_id=task_run.id,
    )


def test_reset_retry_budget_requeues_failed_media_thumbnail_tasks(client, account_user):
    token = _login(client, account_user.username)
    first_media = _create_media("ABC-001")
    second_media = _create_media("ABC-002")
    _create_task_state(first_media)
    _create_task_state(second_media)
    ResourceTaskState.update(
        extra={"deferred_count": 5, "deferred_reason": "115 视频转码尚未完成"}
    ).where(ResourceTaskState.resource_id == first_media.id).execute()

    response = client.post(
        ACTION_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "task_key": THUMBNAIL_TASK_KEY,
            "action": "reset_retry_budget",
            "resource_ids": [first_media.id, second_media.id],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "task_key": THUMBNAIL_TASK_KEY,
        "action": "reset_retry_budget",
        "task_run_id": None,
        "accepted_resource_ids": [first_media.id, second_media.id],
        "skipped": [],
    }
    for media in (first_media, second_media):
        record = ResourceTaskState.get(
            ResourceTaskState.task_key == THUMBNAIL_TASK_KEY,
            ResourceTaskState.resource_id == media.id,
        )
        assert record.state == "pending"
        assert record.attempt_count == 0
        assert record.last_error is None
        assert record.error_code is None
        assert record.next_retry_at is None
        assert record.last_trigger_type == "manual"
        # kernel 记账：重置即重开预算，轮次 +1；尝试历史保留在 attempt 表。
        assert record.retry_round == 1
        assert record.extra is None


def test_reset_skips_unqualified_media_and_requeues_the_rest(client, account_user):
    token = _login(client, account_user.username)
    failed_media = _create_media("ABC-003")
    pending_media = _create_media("ABC-004")
    invalid_media = _create_media("ABC-005", valid=False)
    deleted_media = _create_media("ABC-006")
    _create_task_state(failed_media)
    _create_task_state(pending_media, state="pending")
    _create_task_state(invalid_media, state="failed_retryable")
    _create_task_state(deleted_media, state="exhausted")
    # 只删媒体、留下任务记录，复现巡检/外部删除后残留的孤儿记录。
    deleted_media_id = deleted_media.id
    Media.delete().where(Media.id == deleted_media_id).execute()
    missing_state_resource_id = deleted_media_id + 10_000

    response = client.post(
        ACTION_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "task_key": THUMBNAIL_TASK_KEY,
            "action": "reset_retry_budget",
            "resource_ids": [
                failed_media.id,
                pending_media.id,
                invalid_media.id,
                deleted_media_id,
                missing_state_resource_id,
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "task_key": THUMBNAIL_TASK_KEY,
        "action": "reset_retry_budget",
        "task_run_id": None,
        "accepted_resource_ids": [failed_media.id],
        "skipped": [
            {"resource_id": pending_media.id, "reason": "state_not_actionable"},
            {"resource_id": invalid_media.id, "reason": "media_invalid"},
            {"resource_id": deleted_media_id, "reason": "media_not_found"},
            {"resource_id": missing_state_resource_id, "reason": "media_not_found"},
        ],
    }

    reset_record = ResourceTaskState.get(
        ResourceTaskState.task_key == THUMBNAIL_TASK_KEY,
        ResourceTaskState.resource_id == failed_media.id,
    )
    assert reset_record.state == "pending"
    assert reset_record.attempt_count == 0
    untouched = ResourceTaskState.get(
        ResourceTaskState.task_key == THUMBNAIL_TASK_KEY,
        ResourceTaskState.resource_id == invalid_media.id,
    )
    assert untouched.attempt_count == 2


def test_bulk_reset_by_state_without_resource_ids(client, account_user):
    # 缺省 resource_ids + state 圈定 = 旧订阅页"重置全部已放弃"的对等物。
    token = _login(client, account_user.username)
    exhausted_media = _create_media("ABC-010")
    retryable_media = _create_media("ABC-011")
    _create_task_state(exhausted_media, state="exhausted")
    _create_task_state(retryable_media, state="failed_retryable")

    response = client.post(
        ACTION_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "task_key": THUMBNAIL_TASK_KEY,
            "action": "reset_retry_budget",
            "state": "exhausted",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted_resource_ids"] == [exhausted_media.id]
    assert body["skipped"] == []
    # 状态筛选之外的行不受影响。
    untouched = ResourceTaskState.get(
        ResourceTaskState.task_key == THUMBNAIL_TASK_KEY,
        ResourceTaskState.resource_id == retryable_media.id,
    )
    assert untouched.state == "failed_retryable"


def test_resource_task_list_exposes_deferred_state_from_existing_extra(client, account_user):
    token = _login(client, account_user.username)
    media = _create_media("ABC-015")
    record = _create_task_state(media, state="pending")
    record.extra = {
        "deferred_count": 2,
        "deferred_reason": "115 视频转码尚未完成",
    }
    record.next_retry_at = utc_now_for_db() + timedelta(hours=24)
    record.save()

    response = client.get(
        "/system/resource-task-states",
        headers={"Authorization": f"Bearer {token}"},
        params={"task_key": THUMBNAIL_TASK_KEY, "state": "pending"},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["deferred_count"] == 2
    assert item["deferred_limit"] == 5
    assert item["deferred_reason"] == "115 视频转码尚未完成"
    assert item["next_retry_at"] is not None


def test_rerun_not_supported_for_thumbnail_task(client, account_user):
    token = _login(client, account_user.username)
    media = _create_media("ABC-020")
    _create_task_state(media)

    response = client.post(
        ACTION_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "task_key": THUMBNAIL_TASK_KEY,
            "action": "rerun",
            "resource_ids": [media.id],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "resource_task_action_not_supported"
