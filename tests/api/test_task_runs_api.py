"""任务运行列表接口的回归测试。"""

from datetime import datetime, timedelta

from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_UNSET = object()


def _create_run(
    *,
    task_key: str,
    state: str,
    trigger_type: str = "manual",
    created_at: datetime | None = None,
    finished_at: datetime | None = _UNSET,
    result_summary: dict | None = None,
) -> BackgroundTaskRun:
    created = created_at or utc_now_for_db()
    return BackgroundTaskRun.create(
        task_key=task_key,
        task_name=task_key,
        trigger_type=trigger_type,
        state=state,
        created_at=created,
        started_at=created,
        finished_at=created if finished_at is _UNSET else finished_at,
        result_summary=result_summary or {},
    )


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


def test_get_task_run_by_id_returns_detail(client, account_user):
    token = _login(client, account_user.username)
    task = BackgroundTaskRun.create(
        task_key="library_import",
        task_name="导入任务",
        trigger_type="manual",
        state="completed",
        progress_current=3,
        progress_total=3,
        result_summary={"succeeded_count": 3},
    )

    response = client.get(f"/system/task-runs/{task.id}", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == task.id
    assert body["state"] == "completed"
    assert body["result_summary"] == {"succeeded_count": 3}


def test_get_task_run_by_id_returns_404_for_unknown(client, account_user):
    token = _login(client, account_user.username)

    response = client.get("/system/task-runs/999999", headers=_auth(token))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_run_not_found"



def test_operations_scope_excludes_scheduled_completed_before_pagination(
    client, account_user
):
    token = _login(client, account_user.username)
    now = utc_now_for_db()
    scheduled_completed = _create_run(
        task_key="scheduled_clean",
        state="completed",
        trigger_type="scheduled",
        created_at=now - timedelta(hours=4),
    )
    scheduled_failed = _create_run(
        task_key="scheduled_failed",
        state="failed",
        trigger_type="scheduled",
        created_at=now - timedelta(hours=3),
    )
    manual_completed = _create_run(
        task_key="manual_completed",
        state="completed",
        created_at=now - timedelta(hours=2),
    )
    scheduled_running = _create_run(
        task_key="scheduled_running",
        state="running",
        trigger_type="scheduled",
        created_at=now - timedelta(hours=1),
        finished_at=None,
    )

    headers = _auth(token)
    default_page = client.get("/system/task-runs", headers=headers).json()
    explicit_all = client.get(
        "/system/task-runs", params={"scope": "all"}, headers=headers
    ).json()
    assert default_page["total"] == 4
    assert explicit_all["total"] == 4

    first_page = client.get(
        "/system/task-runs",
        params={"scope": "operations", "page_size": 2, "page": 1},
        headers=headers,
    ).json()
    second_page = client.get(
        "/system/task-runs",
        params={"scope": "operations", "page_size": 2, "page": 2},
        headers=headers,
    ).json()

    assert first_page["total"] == 3
    assert second_page["total"] == 3
    assert len(first_page["items"]) == 2
    assert len(second_page["items"]) == 1
    assert {item["id"] for item in first_page["items"] + second_page["items"]} == {
        scheduled_failed.id,
        manual_completed.id,
        scheduled_running.id,
    }
    assert scheduled_completed.id not in {
        item["id"] for item in first_page["items"] + second_page["items"]
    }

    completed_only = client.get(
        "/system/task-runs",
        params={"scope": "operations", "state": "completed"},
        headers=headers,
    ).json()
    assert [item["id"] for item in completed_only["items"]] == [manual_completed.id]


def test_task_run_scope_rejects_unknown_value(client, account_user):
    token = _login(client, account_user.username)

    response = client.get(
        "/system/task-runs", params={"scope": "ops"}, headers=_auth(token)
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_activity_filter"


def test_activity_bootstrap_task_scope_operations(client, account_user):
    token = _login(client, account_user.username)
    now = utc_now_for_db()
    _create_run(
        task_key="bootstrap_scheduled_clean",
        state="completed",
        trigger_type="scheduled",
        created_at=now - timedelta(hours=2),
    )
    manual = _create_run(
        task_key="bootstrap_manual",
        state="completed",
        created_at=now - timedelta(hours=1),
    )
    headers = _auth(token)

    default_body = client.get("/system/activity/bootstrap", headers=headers).json()
    scoped_body = client.get(
        "/system/activity/bootstrap",
        params={"task_scope": "operations"},
        headers=headers,
    ).json()
    invalid = client.get(
        "/system/activity/bootstrap",
        params={"task_scope": "ops"},
        headers=headers,
    )

    assert default_body["task_runs"]["total"] == 2
    assert scoped_body["task_runs"]["total"] == 1
    assert [item["id"] for item in scoped_body["task_runs"]["items"]] == [manual.id]
    assert invalid.status_code == 422


def test_task_run_has_warning_reflects_failed_counters(client, account_user):
    token = _login(client, account_user.username)
    warned = _create_run(
        task_key="warned_run",
        state="completed",
        result_summary={"succeeded_count": 5, "failed_count": 2},
    )
    clean = _create_run(
        task_key="clean_run",
        state="completed",
        result_summary={"succeeded_count": 5},
    )
    headers = _auth(token)

    detail = client.get(f"/system/task-runs/{warned.id}", headers=headers).json()
    listing = client.get("/system/task-runs", headers=headers).json()

    assert detail["has_warning"] is True
    flags = {item["id"]: item["has_warning"] for item in listing["items"]}
    assert flags[warned.id] is True
    assert flags[clean.id] is False


def test_attention_lists_latest_terminal_issue_per_task(client, account_user):
    token = _login(client, account_user.username)
    now = utc_now_for_db()
    failed = _create_run(
        task_key="attention_failed",
        state="failed",
        trigger_type="scheduled",
        created_at=now - timedelta(hours=5),
        finished_at=now - timedelta(hours=4),
    )
    warned = _create_run(
        task_key="attention_warned",
        state="completed",
        created_at=now - timedelta(hours=3),
        finished_at=now - timedelta(hours=2),
        result_summary={"failed_items": 2, "succeeded_count": 1},
    )
    # 老失败之后的干净完成：最新终态正常，不再进待关注。
    _create_run(
        task_key="attention_recovered",
        state="failed",
        created_at=now - timedelta(days=2),
    )
    _create_run(
        task_key="attention_recovered",
        state="completed",
        created_at=now - timedelta(hours=1),
    )
    # 定时任务正常完成属于常态，不进入待关注。
    _create_run(
        task_key="attention_routine",
        state="completed",
        trigger_type="scheduled",
        created_at=now - timedelta(hours=1),
    )
    # 7 天窗口之外的历史失败不计入 failed_count_7d。
    _create_run(
        task_key="attention_window",
        state="failed",
        created_at=now - timedelta(days=10),
    )
    window_latest = _create_run(
        task_key="attention_window",
        state="failed",
        created_at=now - timedelta(days=1),
    )

    response = client.get("/system/task-runs/attention", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert [item["task_key"] for item in body] == [
        "attention_warned",
        "attention_failed",
        "attention_window",
    ]
    assert set(body[0]) == {
        "task_key",
        "task_name",
        "latest_run",
        "failed_count_7d",
        "last_issue_at",
    }
    assert body[0]["latest_run"]["id"] == warned.id
    assert body[0]["latest_run"]["has_warning"] is True
    assert body[0]["failed_count_7d"] == 0
    assert body[1]["latest_run"]["id"] == failed.id
    assert body[1]["failed_count_7d"] == 1
    assert body[2]["latest_run"]["id"] == window_latest.id
    assert body[2]["failed_count_7d"] == 1


def test_attention_respects_limit_and_rejects_out_of_range(client, account_user):
    token = _login(client, account_user.username)
    now = utc_now_for_db()
    for offset in range(3):
        _create_run(
            task_key=f"attention_limit_{offset}",
            state="failed",
            created_at=now - timedelta(hours=offset + 1),
        )
    headers = _auth(token)

    limited = client.get(
        "/system/task-runs/attention", params={"limit": 2}, headers=headers
    )
    zero = client.get(
        "/system/task-runs/attention", params={"limit": 0}, headers=headers
    )
    too_many = client.get(
        "/system/task-runs/attention", params={"limit": 101}, headers=headers
    )

    assert limited.status_code == 200
    assert [item["task_key"] for item in limited.json()] == [
        "attention_limit_0",
        "attention_limit_1",
    ]
    assert zero.status_code == 422
    assert too_many.status_code == 422
