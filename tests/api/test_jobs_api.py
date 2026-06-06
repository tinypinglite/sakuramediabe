from src.model import BackgroundTaskRun
from src.service.system import ActivityService, TaskRunConflictError


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def test_jobs_endpoints_require_authentication(client):
    assert client.get("/system/jobs").status_code == 401
    assert client.post("/system/jobs/ranking_sync/run").status_code == 401


def test_list_jobs_returns_registry_metadata_and_latest_task_run(client, account_user):
    token = _login(client, username=account_user.username)
    older_task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
    )
    ActivityService.complete_task_run(older_task_run.id, result_summary={"stored_items": 8})
    latest_task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="manual",
    )

    response = client.get(
        "/system/jobs",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    ranking_job = next(item for item in payload if item["task_key"] == "ranking_sync")
    assert ranking_job["log_name"] == "ranking-sync"
    assert ranking_job["cli_name"] == "sync-rankings"
    assert ranking_job["manual_trigger_allowed"] is True
    assert ranking_job["last_task_run"]["id"] == latest_task_run.id
    assert ranking_job["last_task_run"]["trigger_type"] == "manual"


def test_list_jobs_picks_latest_run_per_key_across_multiple_keys(client, account_user):
    # 跨多个 task_key 各自堆叠新旧记录，校验每个 key 都只返回最新一条，不会串台。
    token = _login(client, username=account_user.username)
    expected_latest: dict[str, int] = {}
    for task_key in ("ranking_sync", "movie_heat_update"):
        ActivityService.create_task_run(task_key=task_key, trigger_type="scheduled")
        latest = ActivityService.create_task_run(task_key=task_key, trigger_type="manual")
        expected_latest[task_key] = latest.id

    response = client.get(
        "/system/jobs",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    by_key = {item["task_key"]: item for item in response.json()}
    for task_key, latest_id in expected_latest.items():
        assert by_key[task_key]["last_task_run"]["id"] == latest_id
        assert by_key[task_key]["last_task_run"]["trigger_type"] == "manual"


def test_trigger_job_starts_manual_task_run(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    captured = {}

    def fake_submit_manual_job(job_def):
        captured["task_key"] = job_def.task_key
        return ActivityService.create_task_run(
            task_key=job_def.task_key,
            trigger_type="manual",
        )

    monkeypatch.setattr("src.api.routers.system.jobs.submit_manual_job", fake_submit_manual_job)

    response = client.post(
        "/system/jobs/ranking_sync/run",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert captured == {"task_key": "ranking_sync"}
    assert payload["task_key"] == "ranking_sync"
    assert payload["state"] == "pending"
    assert BackgroundTaskRun.get_by_id(payload["task_run_id"]).trigger_type == "manual"


def test_trigger_job_rejects_unknown_or_forbidden_jobs(client, account_user):
    token = _login(client, username=account_user.username)

    missing_response = client.post(
        "/system/jobs/not_exists/run",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "job_not_found"


def test_trigger_job_returns_conflict_when_same_task_is_running(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    blocking_task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
        state="running",
        mutex_key="aps:ranking_sync",
    )

    def fake_submit_manual_job(job_def):
        raise TaskRunConflictError(blocking_task_run)

    monkeypatch.setattr("src.api.routers.system.jobs.submit_manual_job", fake_submit_manual_job)

    response = client.post(
        "/system/jobs/ranking_sync/run",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["error"]["code"] == "task_conflict"
    assert payload["error"]["details"] == {
        "blocking_task_run_id": blocking_task_run.id,
        "blocking_trigger_type": "scheduled",
        "blocking_state": "running",
    }
