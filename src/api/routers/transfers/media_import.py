from fastapi import APIRouter, Depends, Query, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.transfers.media_import import (
    DeleteFailedFileRequest,
    FilesystemListResponse,
    ImportJobCreateRequest,
    ImportJobListItemResource,
    ImportJobResource,
    ImportJobTriggerResponse,
    RenameFailedFileRequest,
    RetryFailedFilesRequest,
)
from src.service.transfers import FilesystemBrowseService, MediaImportJobService

router = APIRouter(
    tags=["media-import"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("/filesystem/entries", response_model=FilesystemListResponse)
def list_filesystem_entries(path: str | None = Query(default=None)):
    return FilesystemBrowseService.list_entries(path)


@router.post(
    "/import-jobs",
    response_model=ImportJobTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_import_job(payload: ImportJobCreateRequest):
    return MediaImportJobService.trigger_directory_import(
        payload.library_id,
        payload.source_path,
        transfer_mode=payload.transfer_mode,
    )


@router.get("/import-jobs", response_model=PageResponse[ImportJobListItemResource])
def list_import_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return MediaImportJobService.list_jobs(page=page, page_size=page_size)


@router.get("/import-jobs/{import_job_id}", response_model=ImportJobResource)
def get_import_job(import_job_id: int):
    return MediaImportJobService.get_job(import_job_id)


@router.post("/import-jobs/{import_job_id}/retry", response_model=ImportJobTriggerResponse)
def retry_import_job_failed_files(import_job_id: int, payload: RetryFailedFilesRequest):
    return MediaImportJobService.retry_failed_files(import_job_id, payload.files)


@router.delete("/import-jobs/{import_job_id}/failed-files", response_model=ImportJobResource)
def delete_import_job_failed_file(import_job_id: int, payload: DeleteFailedFileRequest):
    return MediaImportJobService.delete_failed_file(import_job_id, payload.path)


@router.post(
    "/import-jobs/{import_job_id}/failed-files/rename",
    response_model=ImportJobResource,
)
def rename_import_job_failed_file(import_job_id: int, payload: RenameFailedFileRequest):
    return MediaImportJobService.rename_failed_file(
        import_job_id,
        payload.path,
        payload.new_name,
    )
