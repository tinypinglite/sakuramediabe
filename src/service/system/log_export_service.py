from __future__ import annotations

import re
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from src.common.runtime_time import get_runtime_timezone, runtime_now
from src.schema.system.logs import LogExportRange

# supervisord 固定把 api/aps 的 stdout/stderr 写到该目录。
LOG_DIR = Path("/data/logs")
LOG_SOURCE_NAMES = ("api.log", "aps.log")
# 与 supervisord.conf 的 stdout_logfile_backups=10 保持一致。
LOG_BACKUP_COUNT = 10
MAX_FILTERED_CONTENT_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024
_REDACTED = "[REDACTED]"

_RANGE_DELTAS = {
    LogExportRange.LAST_HOUR: timedelta(hours=1),
    LogExportRange.LAST_24_HOURS: timedelta(hours=24),
    LogExportRange.LAST_7_DAYS: timedelta(days=7),
    LogExportRange.LAST_30_DAYS: timedelta(days=30),
}
_LOG_TIMESTAMP_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[.,]\d+)?"
)
_HEADER_PATTERN = re.compile(
    r"(?i)\b(Authorization|Proxy-Authorization|Cookie|Set-Cookie)\s*:\s*[^\r\n]*"
)
_SENSITIVE_KEY_PATTERN = (
    r"(?:authorization|access[_-]?token|refresh[_-]?token|id[_-]?token|token|"
    r"password|passwd|pwd|secret|client[_-]?secret|secret[_-]?key|api[_-]?key|"
    r"apikey|access[_-]?key|private[_-]?key|signing[_-]?key|signature|sig)"
)
_KEY_VALUE_PATTERN = re.compile(
    rf"(?i)(?<![A-Za-z0-9])(?P<key>[\"']?{_SENSITIVE_KEY_PATTERN}[\"']?)"
    r"(?![A-Za-z0-9_])(?P<separator>\s*[=:]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s&,;}\]]+)"
)
_BEARER_PATTERN = re.compile(
    r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}"
)
_URL_USERINFO_PATTERN = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s@]+@"
)
_API_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"
)


class LogsUnavailableError(RuntimeError):
    pass


class LogEntriesNotFoundError(RuntimeError):
    pass


class LogExportTooLargeError(RuntimeError):
    pass


@dataclass
class LogExportArchive:
    filename: str
    stream: BinaryIO
    size_bytes: int


class _SeekableSpooledTemporaryFile:
    """补 Python 3.10 的 SpooledTemporaryFile 缺少 seekable 属性的问题。"""

    def __init__(self) -> None:
        self._file = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=_SPOOL_MEMORY_BYTES,
            mode="w+b",
        )

    def seekable(self) -> bool:
        return True

    def __getattr__(self, name: str):
        return getattr(self._file, name)


def redact_log_text(text: str) -> str:
    """屏蔽日志中的凭据与访问签名，不改动其余上下文。"""

    text = _HEADER_PATTERN.sub(
        lambda match: f"{match.group(1)}: {_REDACTED}",
        text,
    )
    text = _KEY_VALUE_PATTERN.sub(_replace_sensitive_value, text)
    text = _BEARER_PATTERN.sub(
        lambda match: f"{match.group(1)} {_REDACTED}",
        text,
    )
    text = _URL_USERINFO_PATTERN.sub(
        lambda match: f"{match.group('scheme')}{_REDACTED}@",
        text,
    )
    text = _API_KEY_PATTERN.sub(_REDACTED, text)
    return _JWT_PATTERN.sub(_REDACTED, text)


def _replace_sensitive_value(match: re.Match[str]) -> str:
    value = match.group("value")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = f"{value[0]}{_REDACTED}{value[0]}"
    else:
        value = _REDACTED
    return f"{match.group('key')}{match.group('separator')}{value}"


class LogExportService:
    @classmethod
    def build_archive(
        cls,
        *,
        export_range: LogExportRange,
        redact_sensitive: bool,
        now: datetime | None = None,
    ) -> LogExportArchive:
        if not LOG_DIR.is_dir():
            raise LogsUnavailableError("log directory is unavailable")

        reference_time = (now or runtime_now()).replace(tzinfo=None)
        cutoff = reference_time - _RANGE_DELTAS[export_range]
        cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_epoch = cutoff.replace(tzinfo=get_runtime_timezone()).timestamp()
        files_by_source = {
            source: cls._resolve_log_files(source) for source in LOG_SOURCE_NAMES
        }

        spool = _SeekableSpooledTemporaryFile()
        try:
            filtered_bytes = 0
            with zipfile.ZipFile(
                spool,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for source in LOG_SOURCE_NAMES:
                    filtered_bytes += cls._append_source(
                        archive,
                        source_name=source,
                        paths=files_by_source[source],
                        cutoff_text=cutoff_text,
                        cutoff_epoch=cutoff_epoch,
                        redact_sensitive=redact_sensitive,
                        filtered_bytes=filtered_bytes,
                    )

            if filtered_bytes == 0:
                raise LogEntriesNotFoundError("no log entries in range")

            archive_size = spool.tell()
            if archive_size > MAX_ARCHIVE_BYTES:
                raise LogExportTooLargeError("log archive exceeds size limit")
            spool.seek(0)
            return LogExportArchive(
                filename=cls._archive_filename(export_range, reference_time),
                stream=spool,
                size_bytes=archive_size,
            )
        except Exception:
            spool.close()
            raise

    @staticmethod
    def _archive_filename(
        export_range: LogExportRange,
        reference_time: datetime,
    ) -> str:
        timestamp = reference_time.strftime("%Y%m%d-%H%M%S")
        return f"sakuramedia-logs-{export_range.value}-{timestamp}.zip"

    @staticmethod
    def _resolve_log_files(source: str) -> list[Path]:
        # 不枚举目录：线上日志目录可能只授予 other 执行位，固定文件名探测更稳。
        paths: list[Path] = []
        for index in range(LOG_BACKUP_COUNT, 0, -1):
            backup = LOG_DIR / f"{source}.{index}"
            if backup.is_file():
                paths.append(backup)
        active = LOG_DIR / source
        if active.is_file():
            paths.append(active)
        return paths

    @classmethod
    def _append_source(
        cls,
        archive: zipfile.ZipFile,
        *,
        source_name: str,
        paths: list[Path],
        cutoff_text: str,
        cutoff_epoch: float,
        redact_sensitive: bool,
        filtered_bytes: int,
    ) -> int:
        written_bytes = 0
        entry: zipfile.ZipExtFile | None = None
        try:
            for line in cls._iter_included_lines(
                paths,
                cutoff_text=cutoff_text,
                cutoff_epoch=cutoff_epoch,
            ):
                payload = redact_log_text(line) if redact_sensitive else line
                encoded = payload.encode("utf-8")
                if (
                    filtered_bytes + written_bytes + len(encoded)
                    > MAX_FILTERED_CONTENT_BYTES
                ):
                    raise LogExportTooLargeError("log content exceeds size limit")
                if entry is None:
                    entry = archive.open(source_name, mode="w", force_zip64=True)
                entry.write(encoded)
                written_bytes += len(encoded)
        finally:
            if entry is not None:
                entry.close()
        return written_bytes

    @staticmethod
    def _iter_included_lines(
        paths: list[Path],
        *,
        cutoff_text: str,
        cutoff_epoch: float,
    ) -> Iterator[str]:
        for path in paths:
            include_current_record = False
            try:
                if path.stat().st_mtime < cutoff_epoch:
                    continue
                with path.open(
                    "r",
                    encoding="utf-8",
                    errors="replace",
                    newline="",
                ) as handle:
                    for line in handle:
                        timestamp_match = _LOG_TIMESTAMP_PATTERN.match(line)
                        if timestamp_match is not None:
                            include_current_record = (
                                timestamp_match.group(1) >= cutoff_text
                            )
                        if include_current_record:
                            yield line
            except FileNotFoundError:
                # 轮转可能刚好在发现文件与打开文件之间发生。
                continue
            except OSError as exc:
                raise LogsUnavailableError("log file is unavailable") from exc

    @classmethod
    def iter_archive_chunks(cls, archive: LogExportArchive) -> Iterator[bytes]:
        try:
            while chunk := archive.stream.read(_READ_CHUNK_BYTES):
                yield chunk
        finally:
            archive.stream.close()
