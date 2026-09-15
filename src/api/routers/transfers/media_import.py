from fastapi import APIRouter, Depends, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.transfers.media_import import (
    ImportAcceptedResponse,
    ImportBrowseRequest,
    ImportBrowseResponse,
    ImportFailedItemResource,
    ImportFailedItemRetryRequest,
    ImportMetadataSearchRequest,
    ImportMetadataSearchResponse,
    ImportRequest,
)
from src.service.transfers.imports.provider_browse_service import ProviderBrowseService
from src.service.transfers.shared.import_task_service import ImportTaskService

router = APIRouter(
    tags=["media-import"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post("/import-sources/browse", response_model=ImportBrowseResponse)
def browse_import_sources(payload: ImportBrowseRequest):
    return ProviderBrowseService.browse(payload)


@router.post(
    "/imports",
    response_model=ImportAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_import(payload: ImportRequest):
    return ImportTaskService.enqueue(payload)


@router.get(
    "/imports/{task_run_id}/failed-items",
    response_model=list[ImportFailedItemResource],
)
def list_import_failed_items(task_run_id: int):
    return ImportTaskService.list_failed_items(task_run_id)


@router.post(
    "/imports/{task_run_id}/failed-items/{item_id}/search",
    response_model=ImportMetadataSearchResponse,
)
def search_import_failed_item(
    task_run_id: int,
    item_id: str,
    payload: ImportMetadataSearchRequest,
):
    return ImportTaskService.search_failed_item(
        task_run_id,
        item_id,
        payload.movie_number,
    )


@router.post(
    "/imports/{task_run_id}/failed-items/{item_id}/retry",
    response_model=ImportAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_import_failed_item(
    task_run_id: int,
    item_id: str,
    payload: ImportFailedItemRetryRequest,
):
    return ImportTaskService.enqueue_failed_item_retry(
        task_run_id,
        item_id,
        payload.candidate_id,
    )
