def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def test_activity_endpoints_require_authentication(client):
    assert client.get("/system/activity/bootstrap").status_code == 401
    assert client.get("/system/notifications").status_code == 401
    assert client.patch("/system/notifications/1/read").status_code == 401
    assert client.get("/system/task-runs").status_code == 401
    assert client.get("/system/events/stream").status_code == 401


def test_activity_api_lists_notifications_and_task_runs(client, account_user):
    token = _login(client, username=account_user.username)

    from src.service.system import ActivityService

    task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
    )
    ActivityService.complete_task_run(
        task_run.id,
        result_summary={"total_targets": 3, "success_targets": 3},
    )
    ActivityService.create_notification(
        category="reminder",
        title="有新的影片可以播放了",
        content="新增 1 部影片",
    )

    notifications_response = client.get(
        "/system/notifications",
        headers={"Authorization": f"Bearer {token}"},
    )
    task_runs_response = client.get(
        "/system/task-runs",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert notifications_response.status_code == 200
    assert notifications_response.json()["total"] == 2
    assert "Z" not in notifications_response.json()["items"][0]["created_at"]
    assert task_runs_response.status_code == 200
    assert task_runs_response.json()["items"][0]["task_key"] == "ranking_sync"
    assert "Z" not in task_runs_response.json()["items"][0]["created_at"]


def test_activity_api_bootstrap_returns_complete_snapshot(client, account_user):
    token = _login(client, username=account_user.username)

    from src.service.system import ActivityService

    task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
        state="running",
    )
    ActivityService.create_notification(
        category="reminder",
        title="有新的影片可以播放了",
        content="新增 1 部影片",
    )

    response = client.get(
        "/system/activity/bootstrap",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["latest_event_id"] > 0
    assert payload["notifications"]["page"] == 1
    assert payload["notifications"]["page_size"] == 20
    assert payload["notifications"]["total"] == 1
    assert payload["unread_count"] == 1
    assert len(payload["active_task_runs"]) == 1
    assert payload["active_task_runs"][0]["id"] == task_run.id
    assert payload["task_runs"]["page"] == 1
    assert payload["task_runs"]["page_size"] == 20
    assert payload["task_runs"]["total"] == 1


def test_activity_api_bootstrap_returns_zero_when_event_table_is_empty(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/system/activity/bootstrap",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["latest_event_id"] == 0


def test_activity_api_bootstrap_filters_match_existing_endpoints(client, account_user):
    token = _login(client, username=account_user.username)

    from src.service.system import ActivityService

    running_task = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
        state="running",
    )
    ActivityService.create_task_run(
        task_key="movie_heat_update",
        trigger_type="manual",
        state="completed",
    )
    ActivityService.create_notification(
        category="reminder",
        title="提醒",
        content="提醒内容",
    )
    ActivityService.create_notification(
        category="warning",
        title="结果",
        content="结果内容",
    )

    bootstrap_response = client.get(
        "/system/activity/bootstrap",
        params={
            "notification_category": "reminder",
            "task_state": "running",
            "task_key": "ranking_sync",
            "task_trigger_type": "scheduled",
            "task_sort": "started_at:desc",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    notifications_response = client.get(
        "/system/notifications",
        params={"category": "reminder"},
        headers={"Authorization": f"Bearer {token}"},
    )
    task_runs_response = client.get(
        "/system/task-runs",
        params={
            "state": "running",
            "task_key": "ranking_sync",
            "trigger_type": "scheduled",
            "sort": "started_at:desc",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert bootstrap_response.status_code == 200
    assert bootstrap_response.json()["notifications"] == notifications_response.json()
    assert bootstrap_response.json()["task_runs"] == task_runs_response.json()
    assert bootstrap_response.json()["active_task_runs"][0]["id"] == running_task.id

def test_activity_sse_endpoint_streams_events(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    def fake_stream(after_event_id=0, poll_interval_seconds=1.0, heartbeat_interval_seconds=15.0):
        yield "id: 1\nevent: notification_created\ndata: {\"title\":\"done\"}\n\n"

    monkeypatch.setattr("src.api.routers.system.activity.SystemEventService.stream", fake_stream)

    response = client.get(
        "/system/events/stream",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: notification_created" in response.text


def test_system_directory_api_filters_blocked_roots(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/system/files/directories",
        headers={"Authorization": f"Bearer {token}"},
        params={"path": "/"},
    )

    assert response.status_code == 200
    names = {item["name"] for item in response.json()["entries"]}
    assert "proc" not in names
    assert "sys" not in names
    assert "tmp" not in names
    assert "usr" not in names


def test_system_directory_api_rejects_unsafe_path(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/system/files/directories",
        headers={"Authorization": f"Bearer {token}"},
        params={"path": "../etc"},
    )

    assert response.status_code == 422


def test_manual_media_import_api_submits_and_cancels_task(client, account_user, tmp_path, monkeypatch):
    token = _login(client, username=account_user.username)
    from src.model import MediaLibrary

    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    submitted = {}

    monkeypatch.setattr(
        "src.service.transfers.manual_media_import_service.BackgroundTaskRunner.submit",
        lambda task_run_id, fn, *args: submitted.update({"task_run_id": task_run_id, "args": args}),
    )

    response = client.post(
        "/system/task-runs/media-import",
        headers={"Authorization": f"Bearer {token}"},
        json={"library_id": library.id, "source_path": str(source_dir)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert submitted["task_run_id"] == payload["task_run_id"]

    cancel_response = client.post(
        f"/system/task-runs/{payload['task_run_id']}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["cancel_requested_at"] is not None


def test_manual_media_import_api_converts_mutex_integrity_error_to_conflict(
    client, account_user, tmp_path, monkeypatch
):
    token = _login(client, username=account_user.username)
    from peewee import IntegrityError
    from src.model import MediaLibrary

    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    def _raise_mutex_conflict(**kwargs):
        raise IntegrityError("unique mutex_key")

    monkeypatch.setattr(
        "src.service.transfers.manual_media_import_service.ActivityService.create_task_run",
        _raise_mutex_conflict,
    )

    response = client.post(
        "/system/task-runs/media-import",
        headers={"Authorization": f"Bearer {token}"},
        json={"library_id": library.id, "source_path": str(source_dir)},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "manual_media_import_conflict"


def test_task_run_items_api_returns_records(client, account_user):
    token = _login(client, username=account_user.username)
    from src.model import BackgroundTaskRun
    from src.service.system import ActivityService, TaskRunItemService

    task_run = ActivityService.create_task_run(
        task_key="manual_media_import",
        trigger_type="manual",
        state="running",
    )
    TaskRunItemService.upsert_item(
        task_run_id=task_run.id,
        item_type="media_import_file",
        item_key="/source/ABC-001.mp4",
        state="succeeded",
        source_path="/source/ABC-001.mp4",
        target_path="/library/ABC-001.mp4",
    )

    response = client.get(
        f"/system/task-runs/{task_run.id}/items",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["target_path"] == "/library/ABC-001.mp4"
