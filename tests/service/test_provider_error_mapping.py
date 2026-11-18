import pytest

from src.plugins.provider_protocol import ProviderOperationError
from src.service.transfers.downloads.client_config_service import (
    _provider_error as client_error,
)
from src.service.transfers.downloads.common import provider_error
from src.service.transfers.downloads.request_service import DownloadRequestService
from src.service.transfers.downloads.sync_service import DownloadSyncService
from src.service.transfers.downloads.task_service import DownloadTaskService
from src.service.transfers.imports.import_service import MediaImportService


@pytest.mark.parametrize(
    "mapper",
    [
        provider_error,
        client_error,
        DownloadRequestService._provider_error,
        DownloadTaskService._provider_error,
        DownloadSyncService._provider_error,
        MediaImportService._provider_error,
    ],
)
@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("authentication_failed", 401),
        ("unavailable", 503),
        ("invalid_config", 422),
        ("unsupported", 422),
        ("source_not_found", 404),
    ],
)
def test_provider_error_mappers_use_bundle_contract_statuses(mapper, code, status_code):
    error = ProviderOperationError(
        provider_key="demo",
        operation="test",
        code=code,
        safe_message="provider failure",
        retryable=True,
    )

    assert mapper(error).status_code == status_code


@pytest.mark.parametrize(
    "mapper",
    [
        provider_error,
        client_error,
        DownloadRequestService._provider_error,
        DownloadTaskService._provider_error,
        DownloadSyncService._provider_error,
    ],
)
def test_download_provider_error_mappers_report_unmanaged_tasks_as_conflicts(mapper):
    error = ProviderOperationError(
        provider_key="demo",
        operation="test",
        code="task_not_managed",
        safe_message="task is not managed",
        retryable=False,
    )

    assert mapper(error).status_code == 409
