from pathlib import Path
from typing import Optional, Sequence, Set
from urllib.parse import urlparse

from peewee import fn

from src.api.exception.errors import ApiError
from src.model import DownloadClient, DownloadTask, Indexer, MediaLibrary

ALLOWED_DOWNLOAD_STATES = {
    "downloading",
    "completed",
    "paused",
    "failed",
    "stalled",
    "checking",
    "queued",
}
ALLOWED_IMPORT_STATUSES = {
    "pending",
    "running",
    "completed",
    "failed",
    "skipped",
}
TASK_SORT_FIELDS = {
    "created_at:desc": (DownloadTask.created_at.desc(), DownloadTask.id.desc()),
    "created_at:asc": (DownloadTask.created_at.asc(), DownloadTask.id.asc()),
    "updated_at:desc": (DownloadTask.updated_at.desc(), DownloadTask.id.desc()),
    "updated_at:asc": (DownloadTask.updated_at.asc(), DownloadTask.id.asc()),
    "progress:desc": (DownloadTask.progress.desc(), DownloadTask.id.desc()),
    "progress:asc": (DownloadTask.progress.asc(), DownloadTask.id.asc()),
}
SYSTEM_QB_TAG = "sakuramedia"
CLIENT_QB_TAG_PREFIX = "client:"


def require_client(client_id: int) -> DownloadClient:
    client = DownloadClient.get_or_none(DownloadClient.id == client_id)
    if client is None:
        raise ApiError(
            404,
            "download_client_not_found",
            "Download client not found",
            {"client_id": client_id},
        )
    return client


def require_media_library(library_id: int) -> MediaLibrary:
    library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
    if library is None:
        raise ApiError(
            404,
            "media_library_not_found",
            "Media library not found",
            {"library_id": library_id},
        )
    return library


def require_indexer(indexer_name: str) -> Indexer:
    normalized = indexer_name.strip()
    if not normalized:
        raise ApiError(
            422,
            "download_request_indexer_not_found",
            "Indexer not found",
            {"indexer_name": indexer_name},
        )
    indexer = Indexer.get_or_none(Indexer.name == normalized)
    if indexer is None:
        raise ApiError(
            422,
            "download_request_indexer_not_found",
            "Indexer not found",
            {"indexer_name": normalized},
        )
    return indexer


def require_task(task_id: int) -> DownloadTask:
    task = DownloadTask.get_or_none(DownloadTask.id == task_id)
    if task is None:
        raise ApiError(
            404,
            "download_task_not_found",
            "Download task not found",
            {"task_id": task_id},
        )
    return task


def validate_non_empty(value: str, code: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ApiError(422, code, message)
    return normalized


def validate_base_url(value: str) -> str:
    normalized = validate_non_empty(
        value,
        "invalid_download_client_base_url",
        "Download client base URL cannot be empty",
    )
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiError(
            422,
            "invalid_download_client_base_url",
            "Download client base URL must use http or https",
        )
    return normalized


def validate_absolute_path(value: str, *, field_name: str) -> str:
    normalized = validate_non_empty(
        value,
        f"invalid_download_client_{field_name}",
        f"{field_name} cannot be empty",
    )
    if not Path(normalized).is_absolute():
        raise ApiError(
            422,
            f"invalid_download_client_{field_name}",
            f"{field_name} must be an absolute path",
        )
    return normalized


def validate_media_library_id(library_id: int) -> int:
    if library_id <= 0:
        raise ApiError(
            422,
            "invalid_download_client_media_library_id",
            "Media library ID must be a positive integer",
        )
    return library_id


def ensure_name_available(name: str, exclude_client_id: Optional[int] = None) -> None:
    query = DownloadClient.select().where(DownloadClient.name == name)
    if exclude_client_id is not None:
        query = query.where(DownloadClient.id != exclude_client_id)
    if query.exists():
        raise ApiError(
            409,
            "download_client_name_conflict",
            "Download client name already exists",
            {"name": name},
        )


def normalize_state_filter(
    value: Optional[str],
    *,
    field_name: str,
    allowed_values: Set[str],
) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in allowed_values:
        raise ApiError(
            422,
            "invalid_download_task_filter",
            f"Invalid {field_name}",
            {field_name: value},
        )
    return normalized


def resolve_task_sort(value: Optional[str]) -> Sequence:
    if value is None:
        return TASK_SORT_FIELDS["created_at:desc"]
    normalized = value.strip().lower()
    if not normalized:
        return TASK_SORT_FIELDS["created_at:desc"]
    if normalized not in TASK_SORT_FIELDS:
        raise ApiError(
            422,
            "invalid_download_task_filter",
            "Invalid sort expression",
            {"sort": value},
        )
    return TASK_SORT_FIELDS[normalized]


def validate_page(page: int, page_size: int) -> None:
    if page <= 0:
        raise ApiError(
            422,
            "invalid_download_task_filter",
            "page must be greater than 0",
            {"page": page},
        )
    if page_size <= 0 or page_size > 100:
        raise ApiError(
            422,
            "invalid_download_task_filter",
            "page_size must be between 1 and 100",
            {"page_size": page_size},
        )


def validate_task_ids(task_ids: Optional[str]) -> list[int]:
    if task_ids is None or not task_ids.strip():
        raise ApiError(
            422,
            "invalid_download_task_ids",
            "task_ids is required",
        )

    values = []
    for raw_part in task_ids.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            raise ApiError(
                422,
                "invalid_download_task_ids",
                "task_ids must be a comma-separated list of positive integers",
                {"task_ids": task_ids},
            )
        values.append(int(part))

    if not values:
        raise ApiError(
            422,
            "invalid_download_task_ids",
            "task_ids must be a comma-separated list of positive integers",
            {"task_ids": task_ids},
        )
    return sorted(set(values))


def build_task_movie_filter(movie_number: str):
    return fn.UPPER(fn.TRIM(DownloadTask.movie)) == movie_number.strip().upper()


def map_remote_path(client: DownloadClient, remote_path: str) -> str:
    normalized_remote = validate_non_empty(
        remote_path,
        "invalid_download_task_save_path",
        "Download task save path cannot be empty",
    )
    if normalized_remote == client.client_save_path:
        return client.local_root_path
    prefix = f"{client.client_save_path.rstrip('/')}/"
    if normalized_remote.startswith(prefix):
        suffix = normalized_remote[len(prefix):]
        return f"{client.local_root_path.rstrip('/')}/{suffix}"
    raise ApiError(
        422,
        "invalid_download_client_path_mapping",
        "Download client path mapping does not match qBittorrent save path",
        {
            "client_id": client.id,
            "remote_path": normalized_remote,
            "client_save_path": client.client_save_path,
        },
    )
