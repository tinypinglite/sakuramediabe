"""任务运行列表接口的回归测试。"""

from datetime import datetime, timedelta

from src.model import BackgroundTaskRun


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_list_active_task_runs_is_not_limited_by_recent_history(client, account_user):
    token = _login(client, account_user.username)
    active_started_at = datetime(2026, 1, 1)
    active = BackgroundTaskRun.create(
        task_key="long_running_task",
        task_name="长时间任务",
        trigger_type="manual",
        state="running",
        progress_current=7,
        progress_total=100,
        started_at=active_started_at,
    )
    for offset in range(101):
        started_at = active_started_at + timedelta(minutes=offset + 1)
        BackgroundTaskRun.create(
            task_key=f"completed_task_{offset}",
            task_name="已完成任务",
            trigger_type="scheduled",
            state="completed",
            started_at=started_at,
            finished_at=started_at,
        )

    response = client.get("/system/task-runs/active", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [active.id]
    assert body[0]["progress_current"] == 7
