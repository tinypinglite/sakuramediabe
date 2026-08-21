from pydantic import BaseModel, ConfigDict

from src.model import BackgroundTaskRun
from src.scheduler.contracts import JobDefinition
from src.scheduler.registry import JOB_REGISTRY_BY_KEY
from src.service.system import ActivityService


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth_headers(client, username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, username)}"}


def _release_task_run(task_run: BackgroundTaskRun) -> None:
    ActivityService.fail_task_run(
        task_run.id,
        error_message="test cleanup",
        notify_result=False,
    )


def test_mixed_job_trigger_preserves_missing_null_empty_and_nonempty_body(
    client, account_user, monkeypatch
):
    class Params(BaseModel):
        model_config = ConfigDict(extra="allow")

    job_def = JobDefinition(
        task_key="demo_http_params",
        log_name="demo-http-params",
        cli_name="demo-http-params",
        cli_help="HTTP params",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: {},
        params_schema=Params,
        params_handler=lambda reporter, params: {},
    ).model_copy(update={"plugin_id": "demo_plugin"})
    monkeypatch.setitem(JOB_REGISTRY_BY_KEY, job_def.task_key, job_def)

    auth_headers = _auth_headers(client, account_user.username)
    endpoint = f"/system/jobs/{job_def.task_key}/run"

    missing = client.post(endpoint, headers=auth_headers)
    assert missing.status_code == 200
    missing_run = BackgroundTaskRun.get_by_id(missing.json()["task_run_id"])
    _release_task_run(missing_run)
    explicit_null = client.post(
        endpoint,
        content="null",
        headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert explicit_null.status_code == 200
    null_run = BackgroundTaskRun.get_by_id(explicit_null.json()["task_run_id"])
    _release_task_run(null_run)
    explicit_empty = client.post(endpoint, json={}, headers=auth_headers)
    assert explicit_empty.status_code == 200
    empty_run = BackgroundTaskRun.get_by_id(explicit_empty.json()["task_run_id"])
    _release_task_run(empty_run)
    explicit_nonempty = client.post(
        endpoint,
        json={"value": 7},
        headers=auth_headers,
    )
    assert explicit_nonempty.status_code == 200
    nonempty_run = BackgroundTaskRun.get_by_id(explicit_nonempty.json()["task_run_id"])

    assert missing_run.params is None
    assert null_run.params is None
    assert empty_run.params == {}
    assert nonempty_run.params == {"value": 7}


def test_handler_only_job_rejects_missing_and_null_body(
    client, account_user, monkeypatch
):
    class EmptyParams(BaseModel):
        pass

    job_def = JobDefinition(
        task_key="demo_http_handler_only",
        log_name="demo-http-handler-only",
        cli_name="demo-http-handler-only",
        cli_help="HTTP handler only",
        manual_only=True,
        params_schema=EmptyParams,
        params_handler=lambda reporter, params: {},
    ).model_copy(update={"plugin_id": "demo_plugin"})
    monkeypatch.setitem(JOB_REGISTRY_BY_KEY, job_def.task_key, job_def)
    auth_headers = _auth_headers(client, account_user.username)
    endpoint = f"/system/jobs/{job_def.task_key}/run"

    missing = client.post(endpoint, headers=auth_headers)
    explicit_null = client.post(
        endpoint,
        content="null",
        headers={**auth_headers, "Content-Type": "application/json"},
    )

    assert missing.status_code == 422
    assert explicit_null.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_job_params"
    assert explicit_null.json()["error"]["code"] == "invalid_job_params"
    assert BackgroundTaskRun.select().count() == 0


def test_factory_only_job_rejects_explicit_body(client, account_user, monkeypatch):
    job_def = JobDefinition(
        task_key="demo_http_factory_only",
        log_name="demo-http-factory-only",
        cli_name="demo-http-factory-only",
        cli_help="HTTP factory only",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: {},
    ).model_copy(update={"plugin_id": "demo_plugin"})
    monkeypatch.setitem(JOB_REGISTRY_BY_KEY, job_def.task_key, job_def)
    auth_headers = _auth_headers(client, account_user.username)
    endpoint = f"/system/jobs/{job_def.task_key}/run"

    explicit_empty = client.post(endpoint, json={}, headers=auth_headers)
    explicit_nonempty = client.post(
        endpoint,
        json={"value": 7},
        headers=auth_headers,
    )

    assert explicit_empty.status_code == 422
    assert explicit_nonempty.status_code == 422
    assert explicit_empty.json()["error"]["code"] == "invalid_job_params"
    assert explicit_nonempty.json()["error"]["code"] == "invalid_job_params"
    assert BackgroundTaskRun.select().count() == 0


def test_builtin_jobs_keep_direct_execution_without_parameter_forms(client, account_user):
    task_keys = (
        "subscribed_movie_auto_download",
        "movie_interaction_sync",
        "media_thumbnail_generation",
    )
    auth_headers = _auth_headers(client, account_user.username)

    for task_key in task_keys:
        job_def = JOB_REGISTRY_BY_KEY[task_key]
        assert job_def.params_schema is None
        assert job_def.params_handler is None
        response = client.post(f"/system/jobs/{task_key}/run", headers=auth_headers)
        assert response.status_code == 200, response.text
        _release_task_run(BackgroundTaskRun.get_by_id(response.json()["task_run_id"]))
