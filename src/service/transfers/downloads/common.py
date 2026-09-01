"""Provider-neutral helpers shared by the downloads services."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy

from src.api.exception.errors import ApiError
from src.common.service_helpers import require_by_id, resolve_sort
from src.model import (
    DownloadClient,
    DownloadTask,
    Indexer,
    IndexerDownloadClient,
    MediaLibrary,
)
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    DownloadClientHandle,
    LibraryHandle,
    ProviderOperationError,
    ProviderUnavailableError,
    RemoteDownloadTask,
)


def library_handle_for(library: MediaLibrary) -> LibraryHandle:
    return LibraryHandle(
        library_id=library.id,
        provider_key=library.provider_key,
        provider_config=deepcopy(library.provider_config or {}),
        account_key=library.account_key,
    )

DOWNLOAD_STATES = {"queued", "downloading", "completed", "failed"}
DOWNLOAD_COMPLETE_STATES = {"completed"}
TASK_SORT_FIELDS = {
    "created_at:desc": (DownloadTask.created_at.desc(), DownloadTask.id.desc()),
    "created_at:asc": (DownloadTask.created_at.asc(), DownloadTask.id.asc()),
    "updated_at:desc": (DownloadTask.updated_at.desc(), DownloadTask.id.desc()),
    "updated_at:asc": (DownloadTask.updated_at.asc(), DownloadTask.id.asc()),
    "progress:desc": (DownloadTask.progress.desc(), DownloadTask.id.desc()),
    "progress:asc": (DownloadTask.progress.asc(), DownloadTask.id.asc()),
}


def require_client(client_id: int) -> DownloadClient:
    return require_by_id(
        DownloadClient,
        client_id,
        "download_client",
        error_message="Download client not found",
        error_details_key="client_id",
    )


def require_library(library_id: int) -> MediaLibrary:
    return require_by_id(
        MediaLibrary,
        library_id,
        "media_library",
        error_message="Media library not found",
        error_details_key="library_id",
    )


def require_task(task_id: int) -> DownloadTask:
    return require_by_id(
        DownloadTask,
        task_id,
        "download_task",
        error_message="Download task not found",
        error_details_key="task_id",
    )


def library_provider(library: MediaLibrary):
    try:
        return MEDIA_PROVIDER_REGISTRY.require(library.provider_key)
    except ProviderUnavailableError as exc:
        raise ApiError(
            503,
            "provider_not_installed",
            "媒体提供方未安装",
            {"provider_key": library.provider_key},
        ) from exc


def download_client_handle(client: DownloadClient) -> DownloadClientHandle:
    return DownloadClientHandle(
        client_id=client.id,
        library=library_handle_for(client.library),
        provider_config=deepcopy(client.provider_config or {}),
    )


def download_provider(client: DownloadClient):
    # ``download_for`` performs structural validation at the call boundary.  No
    # provider-specific branch or fallback belongs in the host.
    try:
        return MEDIA_PROVIDER_REGISTRY.download_for(download_client_handle(client))
    except ProviderUnavailableError as exc:
        raise ApiError(
            503,
            "provider_not_installed",
            "媒体提供方未安装",
            {"provider_key": client.library.provider_key},
        ) from exc
    except ProviderOperationError as exc:
        raise provider_error(exc) from exc


def provider_error(exc: ProviderOperationError, *, operation: str | None = None) -> ApiError:
    status_code = {
        "invalid_config": 422,
        "authentication_failed": 401,
        "source_not_found": 404,
        "task_not_managed": 409,
        "source_blacklisted": 422,
        "unsupported": 422,
        "unavailable": 503,
    }.get(exc.code, 502)
    return ApiError(
        status_code,
        f"provider_{exc.code}",
        exc.safe_message,
        {"provider_key": exc.provider_key, "operation": operation or exc.operation},
    )


def validate_non_empty(value: str, code: str, message: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ApiError(422, code, message)
    return normalized


def validate_remote_download_task(remote_task: object) -> RemoteDownloadTask:
    """Validate the small provider result shape before writing the task ledger."""
    if not isinstance(remote_task, RemoteDownloadTask):
        raise ApiError(502, "provider_invalid_response", "下载提供方返回了无效任务")
    if remote_task.state == "completed" and not remote_task.completed_source_ref:
        raise ApiError(502, "provider_invalid_response", "已完成下载缺少来源引用")
    return remote_task


def list_indexer_clients(indexer: Indexer) -> list[DownloadClient]:
    return [
        link.download_client
        for link in (
            IndexerDownloadClient.select(IndexerDownloadClient, DownloadClient)
            .join(DownloadClient)
            .where(IndexerDownloadClient.indexer == indexer.id)
            .order_by(IndexerDownloadClient.id.asc())
        )
    ]


def resolve_preferred_client(clients: Sequence[DownloadClient]) -> DownloadClient:
    if not clients:
        raise ApiError(
            422,
            "download_request_client_resolution_failed",
            "Indexer has no bound download clients",
        )
    # Binding order is the only host-visible ordering.  Provider capability
    # or kind tables would duplicate bundle knowledge in the host.
    return clients[0]


def require_indexer(indexer_name: str) -> Indexer:
    normalized = (indexer_name or "").strip()
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


def normalize_state_filters(
    values: list[str] | None,
    *,
    field_name: str,
    allowed_values: set[str] = DOWNLOAD_STATES,
) -> set[str] | None:
    if not values:
        return None
    normalized: set[str] = set()
    for value in values:
        item = (value or "").strip().lower()
        if not item:
            continue
        if item not in allowed_values:
            raise ApiError(
                422,
                "invalid_download_task_filter",
                f"Invalid {field_name}",
                {field_name: value},
            )
        normalized.add(item)
    return normalized or None


def resolve_task_sort(value: str | None) -> Sequence:
    return resolve_sort(
        value,
        TASK_SORT_FIELDS,
        default_key="created_at:desc",
        error_code="invalid_download_task_filter",
    )


def build_task_movie_filter(movie_number: str):
    return DownloadTask.movie == (movie_number or "").strip()


def is_download_complete(state: str) -> bool:
    return state in DOWNLOAD_COMPLETE_STATES
