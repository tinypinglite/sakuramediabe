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
    assert client.patch("/system/notifications/1/archive").status_code == 401
    assert client.get("/system/notifications/unread-count").status_code == 401
    assert client.get("/system/task-runs").status_code == 401
    assert client.get("/system/task-runs/active").status_code == 401
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


def test_activity_api_marks_notification_read_and_archive(client, account_user):
    token = _login(client, username=account_user.username)

    from src.service.system import ActivityService

    notification = ActivityService.create_notification(
        category="info",
        title="排行榜同步已完成",
        content="total_targets=3 success_targets=3",
    )

    read_response = client.patch(
        f"/system/notifications/{notification.id}/read",
        headers={"Authorization": f"Bearer {token}"},
    )
    archive_response = client.patch(
        f"/system/notifications/{notification.id}/archive",
        headers={"Authorization": f"Bearer {token}"},
    )
    unread_count_response = client.get(
        "/system/notifications/unread-count",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert read_response.status_code == 200
    assert read_response.json()["is_read"] is True
    assert "Z" not in read_response.json()["read_at"]
    assert archive_response.status_code == 200
    assert archive_response.json()["archived"] is True
    assert "Z" not in archive_response.json()["archived_at"]
    assert unread_count_response.json()["unread_count"] == 0


def test_activity_api_lists_active_task_runs(client, account_user):
    token = _login(client, username=account_user.username)

    from src.service.system import ActivityService

    ActivityService.create_task_run(task_key="ranking_sync", trigger_type="scheduled", state="running")
    ActivityService.create_task_run(task_key="movie_heat_update", trigger_type="scheduled", state="completed")

    response = client.get(
        "/system/task-runs/active",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["state"] == "running"


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
