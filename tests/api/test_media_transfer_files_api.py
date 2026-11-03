from src.model import BackgroundTaskRun


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _transfer_file(
    *,
    media_id: int = 11,
    file_name: str = "ABC-001.mp4",
    status: str = "transferred",
    reason_code: str | None = None,
    target_committed: bool = False,
) -> dict:
    return {
        "media_id": media_id,
        "file_name": file_name,
        "movie_number": "ABC-001",
        "size_bytes": 1024,
        "status": status,
        "reason_code": reason_code,
        "target_committed": target_committed,
    }


def test_media_transfer_files_endpoint_returns_file_details(client, account_user):
    task = BackgroundTaskRun.create(
        task_key="media_storage_transfer",
        task_name="媒体存储迁移",
        trigger_type="manual",
        state="completed",
        result_summary={
            "transferred_count": 1,
            "failed_count": 1,
            "files": [
                _transfer_file(target_committed=True),
                _transfer_file(
                    media_id=12,
                    file_name="ABC-002.mp4",
                    status="failed",
                    reason_code="media_busy",
                ),
            ],
        },
    )

    response = client.get(
        f"/media-transfers/{task.id}/files",
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["status"] for item in body] == ["transferred", "failed"]
    assert body[1]["reason_code"] == "media_busy"
    assert body[1]["file_name"] == "ABC-002.mp4"


def test_media_transfer_files_endpoint_rejects_other_task_type(
    client, account_user
):
    task = BackgroundTaskRun.create(
        task_key="library_import",
        task_name="媒体库导入",
        trigger_type="manual",
        state="completed",
        result_summary={"files": [_transfer_file()]},
    )

    response = client.get(
        f"/media-transfers/{task.id}/files",
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "media_transfer_task_not_found"


def test_task_run_resource_replaces_transfer_files_with_count(
    client, account_user
):
    task = BackgroundTaskRun.create(
        task_key="media_storage_transfer",
        task_name="媒体存储迁移",
        trigger_type="manual",
        state="completed",
        result_summary={
            "transferred_count": 1,
            "unexecuted_media_ids": [],
            "files": [_transfer_file()],
        },
    )

    response = client.get(
        f"/system/task-runs/{task.id}",
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    summary = response.json()["result_summary"]
    assert "files" not in summary
    assert summary["file_details"] == {"count": 1}
    assert summary["transferred_count"] == 1
