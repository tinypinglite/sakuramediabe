import os
import zipfile
from datetime import datetime, timedelta

import pytest

from src.schema.system.logs import LogExportRange
from src.service.system import log_export_service
from src.service.system.log_export_service import (
    LogEntriesNotFoundError,
    LogExportService,
    LogExportTooLargeError,
    LogsUnavailableError,
    redact_log_text,
)


def _write_log(path, lines):
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _set_mtime(root, value):
    timestamp = value.timestamp()
    for path in root.iterdir():
        os.utime(path, (timestamp, timestamp))


def _archive_text(archive, entry_name):
    with zipfile.ZipFile(archive.stream) as zipped:
        return zipped.read(entry_name).decode("utf-8")


def test_build_archive_filters_by_time_and_keeps_traceback_continuations(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path)
    now = datetime(2026, 11, 3, 12, 0, 0)
    _write_log(
        tmp_path / "api.log",
        [
            "2026-11-01 11:59:59.999 | INFO | old | old line",
            "2026-11-03 11:30:00.123 | INFO | app:run:1 - recent line",
            "Traceback (most recent call last):",
            '  File "/app/src/example.py", line 1',
            "2026-11-01 10:00:00,000 - INFO - old access",
        ],
    )
    _write_log(
        tmp_path / "api.log.1",
        [
            "2026-11-03 10:30:00.000 | WARNING | app:run:2 - rotated recent",
            "  continuation from rotated",
        ],
    )
    _write_log(
        tmp_path / "aps.log",
        [
            "2026-11-03 11:00:00,100 - INFO - scheduler",
        ],
    )
    _set_mtime(tmp_path, now)

    archive = LogExportService.build_archive(
        export_range=LogExportRange.LAST_24_HOURS,
        redact_sensitive=False,
        now=now,
    )
    try:
        assert archive.filename == "sakuramedia-logs-24h-20261103-120000.zip"
        with zipfile.ZipFile(archive.stream) as zipped:
            assert zipped.namelist() == ["api.log", "aps.log"]
        api_log = _archive_text(archive, "api.log")
        assert "old line" not in api_log
        assert "old access" not in api_log
        assert "rotated recent" in api_log
        assert "continuation from rotated" in api_log
        assert "recent line" in api_log
        assert "Traceback (most recent call last):" in api_log
        assert _archive_text(archive, "aps.log").strip() == (
            "2026-11-03 11:00:00,100 - INFO - scheduler"
        )
    finally:
        archive.stream.close()


@pytest.mark.parametrize(
    ("export_range", "expected"),
    [
        (LogExportRange.LAST_HOUR, False),
        (LogExportRange.LAST_24_HOURS, True),
        (LogExportRange.LAST_7_DAYS, True),
        (LogExportRange.LAST_30_DAYS, True),
    ],
)
def test_build_archive_applies_range_cutoff(
    tmp_path, monkeypatch, export_range, expected
):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path)
    now = datetime(2026, 11, 3, 12, 0, 0)
    line_time = now - timedelta(hours=2)
    _write_log(
        tmp_path / "api.log",
        [f"{line_time:%Y-%m-%d %H:%M:%S}.000 | INFO | sample"],
    )
    _set_mtime(tmp_path, now)

    if expected:
        archive = LogExportService.build_archive(
            export_range=export_range,
            redact_sensitive=False,
            now=now,
        )
        archive.stream.close()
    else:
        with pytest.raises(LogEntriesNotFoundError):
            LogExportService.build_archive(
                export_range=export_range,
                redact_sensitive=False,
                now=now,
            )


def test_build_archive_rejects_empty_range(tmp_path, monkeypatch):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path)
    now = datetime(2026, 11, 3, 12, 0, 0)
    _write_log(
        tmp_path / "api.log",
        ["2026-10-01 00:00:00.000 | INFO | too old"],
    )
    _set_mtime(tmp_path, now)

    with pytest.raises(LogEntriesNotFoundError):
        LogExportService.build_archive(
            export_range=LogExportRange.LAST_24_HOURS,
            redact_sensitive=False,
            now=now,
        )


def test_build_archive_rejects_unavailable_log_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path / "missing")

    with pytest.raises(LogsUnavailableError):
        LogExportService.build_archive(
            export_range=LogExportRange.LAST_24_HOURS,
            redact_sensitive=False,
        )


def test_build_archive_enforces_filtered_content_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path)
    monkeypatch.setattr(log_export_service, "MAX_FILTERED_CONTENT_BYTES", 10)
    now = datetime(2026, 11, 3, 12, 0, 0)
    _write_log(
        tmp_path / "api.log",
        ["2026-11-03 11:00:00.000 | INFO | too long"],
    )
    _set_mtime(tmp_path, now)

    with pytest.raises(LogExportTooLargeError):
        LogExportService.build_archive(
            export_range=LogExportRange.LAST_24_HOURS,
            redact_sensitive=False,
            now=now,
        )


def test_build_archive_enforces_archive_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(log_export_service, "LOG_DIR", tmp_path)
    monkeypatch.setattr(log_export_service, "MAX_ARCHIVE_BYTES", 1)
    now = datetime(2026, 11, 3, 12, 0, 0)
    _write_log(
        tmp_path / "api.log",
        ["2026-11-03 11:00:00.000 | INFO | sample"],
    )
    _set_mtime(tmp_path, now)

    with pytest.raises(LogExportTooLargeError):
        LogExportService.build_archive(
            export_range=LogExportRange.LAST_24_HOURS,
            redact_sensitive=False,
            now=now,
        )


def test_redact_log_text_masks_credentials_but_keeps_diagnostics():
    api_key = "sk-" + "test1234567890"
    jwt = "eyJ" + "aaaaaa.bbbbbb.cccccc"
    source = "\n".join(
        [
            "Authorization: Bearer bearer-token-123456",
            "Cookie: uid=1; seid=2",
            "token=query-token",
            '{"password":"secret","client_secret": "hidden","token_len":42}',
            (
                "https://alice:url-password@example.com/path"
                "?access_token=query-token&signature=deadbeef&ok=1"
                "&signature=deadbeef&ok=1"
            ),
            "Bearer plain-token-123456",
            f"{api_key} {jwt}",
        ]
    )

    redacted = redact_log_text(source)

    assert "bearer-token-123456" not in redacted
    assert "uid=1" not in redacted
    assert "query" not in redacted
    assert "hidden" not in redacted
    assert "url-password" not in redacted
    assert "signature=deadbeef" not in redacted
    assert "plain-token-123456" not in redacted
    assert api_key not in redacted
    assert jwt not in redacted
    assert "token_len" in redacted
    assert "&ok=1" in redacted
    assert "https://[REDACTED]@example.com/path" in redacted
