from __future__ import annotations

from pathlib import Path

import pytest

from src.config.config import settings
from src.model import BackgroundTaskRun, SystemNotification
from src.scheduler.worker import TaskWorker
from src.service.catalog.subtitle_import_service import SubtitleImportService
from src.service.system import ActivityService
from src.service.system.task_queue_service import TaskQueueService


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.parametrize(
    "file_names,statuses,expected_summary,expected_notifications",
    [
        ([], {}, {"imported_count": 0, "skipped_count": 0, "failed_count": 0}, 0),
        (
            ["GOOD-001.srt"],
            {"GOOD-001.srt": "imported"},
            {"imported_count": 1, "skipped_count": 0, "failed_count": 0},
            0,
        ),
        (
            ["GOOD-002.srt", "BAD.srt", "DUP-001.srt"],
            {
                "GOOD-002.srt": "imported",
                "BAD.srt": "failed",
                "DUP-001.srt": "skipped",
            },
            {"imported_count": 1, "skipped_count": 1, "failed_count": 1},
            1,
        ),
    ],
)
def test_subtitle_import_post_publishes_task_run_and_worker_completes_summary(
    client,
    account_user,
    tmp_path,
    monkeypatch,
    file_names,
    statuses,
    expected_summary,
    expected_notifications,
):
    source_dir = tmp_path / "subtitles"
    source_dir.mkdir()
    for file_name in file_names:
        (source_dir / file_name).write_text("1\n00:00:00,000 --> 00:00:01,000\ntext\n")
    monkeypatch.setattr(settings.media_import, "browse_roots", [str(tmp_path)])

    def fake_import_single(self, subtitle_path: Path, *, existing_hashes):
        del self, existing_hashes
        state = statuses[subtitle_path.name]
        return state, f"{state}_reason", subtitle_path.name

    monkeypatch.setattr(
        SubtitleImportService,
        "_import_single_subtitle",
        fake_import_single,
    )

    response = client.post(
        "/subtitle-imports",
        json={"source_path": str(source_dir)},
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 202
    assert set(response.json()) == {"task_run_id", "task_key", "state"}
    assert response.json()["task_key"] == "subtitle_directory_import"
    assert response.json()["state"] == "pending"
    task_run = BackgroundTaskRun.get_by_id(response.json()["task_run_id"])
    assert task_run.params == {"source_path": str(source_dir)}
    assert task_run.scheduled_at is not None

    claimed = TaskQueueService.claim_next()
    assert claimed is not None
    TaskWorker()._execute(claimed)

    stored = BackgroundTaskRun.get_by_id(task_run.id)
    assert stored.state == "completed"
    assert stored.result_summary == expected_summary
    assert "failed_files" not in stored.result_summary
    assert (
        SystemNotification.select()
        .where(SystemNotification.related_task_run == task_run.id)
        .count()
        == expected_notifications
    )


def test_subtitle_import_invalid_path_fails_task_run_with_single_notification(
    client, account_user, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings.media_import, "browse_roots", [str(tmp_path)])
    missing_path = tmp_path / "missing"
    response = client.post(
        "/subtitle-imports",
        json={"source_path": str(missing_path)},
        headers=_auth_headers(client, account_user.username),
    )
    task_run_id = response.json()["task_run_id"]

    claimed = TaskQueueService.claim_next()
    assert claimed is not None
    TaskWorker()._execute(claimed)
    ActivityService.fail_task_run(
        task_run_id, error_message="duplicate terminal callback"
    )

    stored = BackgroundTaskRun.get_by_id(task_run_id)
    assert stored.state == "failed"
    assert stored.mutex_key is None
    assert stored.lease_expires_at is None
    assert (
        SystemNotification.select()
        .where(SystemNotification.related_task_run == task_run_id)
        .count()
        == 1
    )
