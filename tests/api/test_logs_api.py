import io
import zipfile
from datetime import timedelta

from src.common.runtime_time import runtime_now
from src.service.system import log_export_service


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def _auth_headers(client, account_user):
    token = _login(client, username=account_user.username)
    return {"Authorization": f"Bearer {token}"}


def _write_recent_logs(tmp_path):
    now = runtime_now().replace(tzinfo=None)
    recent = f"{now:%Y-%m-%d %H:%M:%S}.000 | INFO | recent"
    old = f"{now - timedelta(days=2):%Y-%m-%d %H:%M:%S}.000 | INFO | old"
    (tmp_path / "api.log").write_text(
        f"{recent} token=secret\n{old}\n",
        encoding="utf-8",
    )
    (tmp_path / "api.log.1").write_text(
        f"{now - timedelta(hours=1):%Y-%m-%d %H:%M:%S}.000 | WARNING | rotated\n",
        encoding="utf-8",
    )
    (tmp_path / "aps.log").write_text(
        f"{now:%Y-%m-%d %H:%M:%S},000 - INFO - scheduler\n",
        encoding="utf-8",
    )


def test_log_archive_requires_authentication(client):
    response = client.get("/system/logs/archive")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_log_archive_returns_filtered_zip(
    client, account_user, tmp_path, monkeypatch
):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path)
    _write_recent_logs(tmp_path)

    response = client.get(
        "/system/logs/archive",
        params={"range": "24h", "redact_sensitive": "true"},
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert "sakuramedia-logs-24h-" in response.headers["content-disposition"]
    assert response.headers["content-length"] == str(len(response.content))
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["api.log", "aps.log"]
        api_log = archive.read("api.log").decode("utf-8")
        assert "rotated" in api_log
        assert "recent" in api_log
        assert "old" not in api_log
        assert "secret" not in api_log
        assert "[REDACTED]" in api_log
        assert "scheduler" in archive.read("aps.log").decode("utf-8")


def test_log_archive_can_disable_redaction(
    client, account_user, tmp_path, monkeypatch
):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path)
    _write_recent_logs(tmp_path)

    response = client.get(
        "/system/logs/archive",
        params={"range": "24h", "redact_sensitive": "false"},
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "token=secret" in archive.read("api.log").decode("utf-8")


def test_log_archive_returns_localized_empty_error(
    client, account_user, tmp_path, monkeypatch
):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path)
    (tmp_path / "api.log").write_text("", encoding="utf-8")

    response = client.get(
        "/system/logs/archive",
        headers={
            **_auth_headers(client, account_user),
            "Accept-Language": "en",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "log_entries_not_found",
        "message": "No logs are available in the selected time range",
        "details": None,
    }


def test_log_archive_returns_unavailable_error(
    client, account_user, tmp_path, monkeypatch
):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path / "missing")

    response = client.get(
        "/system/logs/archive",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "logs_unavailable"


def test_log_archive_returns_too_large_error(
    client, account_user, tmp_path, monkeypatch
):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path)
    monkeypatch.setattr(log_export_service, "MAX_FILTERED_CONTENT_BYTES", 1)
    _write_recent_logs(tmp_path)

    response = client.get(
        "/system/logs/archive",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "log_export_too_large"
