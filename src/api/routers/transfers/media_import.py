from fastapi import APIRouter, Depends, Query, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.transfers.media_import import (
    FilesystemListResponse,
    ImportAcceptedResponse,
    ImportRequest,
)
from src.service.transfers.imports.browse_service import FilesystemBrowseService
from src.service.transfers.shared.import_task_service import ImportTaskService

router = APIRouter(
    tags=["media-import"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("/filesystem/entries", response_model=FilesystemListResponse)
def list_filesystem_entries(path: str | None = Query(default=None)):
    return FilesystemBrowseService.list_entries(path)


@router.post(
    "/imports",
    response_model=ImportAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_import(payload: ImportRequest):
    return ImportTaskService.enqueue(payload)
