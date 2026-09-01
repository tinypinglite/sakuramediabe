"""Submit a candidate through the download component of its provider bundle."""

from __future__ import annotations

from src.api.exception.errors import ApiError
from src.model import DownloadTask
from src.plugins.provider_protocol import DownloadSubmission, ProviderOperationError
from src.schema.transfers.downloads import (
    DownloadRequestCreateRequest,
    DownloadRequestCreateResponse,
    DownloadTaskResource,
)
from src.service.transfers.downloads.common import (
    download_provider,
    list_indexer_clients,
    require_client,
    require_indexer,
    resolve_preferred_client,
    validate_non_empty,
    validate_remote_download_task,
)


class DownloadRequestService:
    def create_request(self, payload: DownloadRequestCreateRequest) -> DownloadRequestCreateResponse:
        client = self._resolve_client(payload)
        movie_number = validate_non_empty(
            payload.movie_number,
            "invalid_download_request_movie_number",
            "movie_number cannot be empty",
        )
        source_uri = validate_non_empty(
            payload.candidate.source_uri,
            "invalid_download_request_candidate",
            "candidate source_uri cannot be empty",
        )
        display_name = validate_non_empty(
            payload.candidate.title,
            "invalid_download_request_candidate",
            "candidate title cannot be empty",
        )
        try:
            remote_task = download_provider(client).submit(
                submission=DownloadSubmission(
                    source_uri=source_uri,
                    display_name=display_name,
                )
            )
        except ProviderOperationError as exc:
            raise self._provider_error(exc) from exc
        remote_task = validate_remote_download_task(remote_task)
        task, created = DownloadTask.get_or_create(
            client=client,
            remote_id=remote_task.remote_id,
            defaults={
                "movie": movie_number,
                "name": remote_task.name or display_name,
                "state": remote_task.state,
                "progress": remote_task.progress,
                "completed_source_ref": remote_task.completed_source_ref,
                "import_status": "pending",
            },
        )
        if not created:
            task.movie = movie_number
            task.name = remote_task.name or display_name
            task.state = remote_task.state
            task.progress = remote_task.progress
            task.completed_source_ref = remote_task.completed_source_ref
            task.save(
                only=[
                    DownloadTask.movie,
                    DownloadTask.name,
                    DownloadTask.state,
                    DownloadTask.progress,
                    DownloadTask.completed_source_ref,
                ]
            )
        return DownloadRequestCreateResponse(
            task=DownloadTaskResource.from_model(task),
            created=created,
        )

    @staticmethod
    def _provider_error(exc: ProviderOperationError) -> ApiError:
        status = {
            "invalid_config": 422,
            "authentication_failed": 401,
            "source_not_found": 404,
            "task_not_managed": 409,
            "source_blacklisted": 422,
            "unsupported": 422,
            "unavailable": 503,
        }.get(exc.code, 502)
        return ApiError(
            status,
            f"provider_{exc.code}",
            exc.safe_message,
            {"provider_key": exc.provider_key, "operation": exc.operation},
        )

    @staticmethod
    def _resolve_client(payload: DownloadRequestCreateRequest):
        indexer = require_indexer(payload.candidate.indexer_name)
        clients = list_indexer_clients(indexer)
        if payload.client_id is None:
            return resolve_preferred_client(clients)
        client = require_client(payload.client_id)
        if any(bound_client.id == client.id for bound_client in clients):
            return client
        raise ApiError(
            422,
            "download_request_client_not_bound_to_indexer",
            "Download client is not bound to candidate indexer",
            {"client_id": client.id, "indexer_name": indexer.name},
        )
