"""批量操作提交接口的回归测试。"""

import pytest

from src.model import BackgroundTaskRun


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_submit_media_delete_creates_pending_batch_operation(client, account_user):
    token = _login(client, account_user.username)

    response = client.post(
        "/batch-operations",
        json={"operation": "media_delete", "ids": [3, 1, 2]},
        headers=_auth(token),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["task_key"] == "batch_operation"
    assert body["state"] == "pending"
    task = BackgroundTaskRun.get_by_id(body["task_run_id"])
    assert task.task_name == "批量删除媒体"
    # 不占 mutex：同 key 多行 pending 即用户任务队列。
    assert task.mutex_key is None
    assert task.params == {
        "operation": "media_delete",
        "ids": [3, 1, 2],
        "target_collection_id": None,
        "delete_files": False,
    }


def test_submit_batch_operations_queue_without_conflict(client, account_user):
    token = _login(client, account_user.username)

    first = client.post(
        "/batch-operations",
        json={"operation": "media_delete", "ids": [1]},
        headers=_auth(token),
    )
    second = client.post(
        "/batch-operations",
        json={"operation": "clip_delete", "ids": [2]},
        headers=_auth(token),
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["task_run_id"] != second.json()["task_run_id"]
    stored = BackgroundTaskRun.select().where(BackgroundTaskRun.state == "pending")
    assert stored.count() == 2
    assert {task.task_name for task in stored} == {"批量删除媒体", "批量删除切片"}


def test_submit_collection_operation_requires_target_collection(client, account_user):
    token = _login(client, account_user.username)

    response = client.post(
        "/batch-operations",
        json={"operation": "video_collection_add", "ids": [1], "target_collection_id": 8},
        headers=_auth(token),
    )

    assert response.status_code == 202
    task = BackgroundTaskRun.get_by_id(response.json()["task_run_id"])
    assert task.task_name == "批量加入视频合集"
    assert task.params["target_collection_id"] == 8


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "media_delete", "ids": []},
        {"operation": "media_delete", "ids": [0]},
        {"operation": "media_delete", "ids": [1, 1]},
        {"operation": "media_delete", "ids": [True]},
        {"operation": "media_delete", "ids": list(range(1, 1002))},
        {"operation": "unknown_operation", "ids": [1]},
        {"operation": "clip_collection_add", "ids": [1]},
        {"operation": "media_delete", "ids": [1], "target_collection_id": 2},
        {"operation": "media_delete", "ids": [1], "delete_files": True},
    ],
)
def test_submit_batch_operation_rejects_invalid_payloads(
    client, account_user, payload
):
    token = _login(client, account_user.username)

    response = client.post(
        "/batch-operations", json=payload, headers=_auth(token)
    )

    assert response.status_code == 422
    assert BackgroundTaskRun.select().count() == 0
