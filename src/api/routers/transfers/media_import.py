from fastapi import APIRouter, Depends, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.transfers.media_import import (
    ImportAcceptedResponse,
    ImportBrowseRequest,
    ImportBrowseResponse,
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
