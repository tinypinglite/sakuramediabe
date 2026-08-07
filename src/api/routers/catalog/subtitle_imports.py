"""手动字幕目录导入接口。"""

from fastapi import APIRouter, Depends, Query, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.subtitle_imports import (
    SubtitleImportCreateRequest,
    SubtitleImportJobListItemResource,
    SubtitleImportJobResource,
    SubtitleImportTriggerResponse,
)
from src.schema.common.pagination import PageResponse
from src.schema.transfers.media_import import (
    DeleteFailedFileRequest,
    RenameFailedFileRequest,
    RetryFailedFilesRequest,
)
from src.service.catalog.subtitle_import_job_service import SubtitleImportJobService

router = APIRouter(
    tags=["subtitle-import"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post(
    "/subtitle-imports",
    response_model=SubtitleImportTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_subtitle_import_job(payload: SubtitleImportCreateRequest):
    return SubtitleImportJobService.trigger_directory_import(payload.source_path)


@router.get(
    "/subtitle-imports",
    response_model=PageResponse[SubtitleImportJobListItemResource],
)
def list_subtitle_import_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return SubtitleImportJobService.list_jobs(page=page, page_size=page_size)


@router.get(
    "/subtitle-imports/{subtitle_import_job_id}",
    response_model=SubtitleImportJobResource,
)
def get_subtitle_import_job(subtitle_import_job_id: int):
    return SubtitleImportJobService.get_job(subtitle_import_job_id)


@router.post(
    "/subtitle-imports/{subtitle_import_job_id}/retry",
    response_model=SubtitleImportTriggerResponse,
)
def retry_subtitle_import_failed_files(
    subtitle_import_job_id: int,
    payload: RetryFailedFilesRequest,
):
    return SubtitleImportJobService.retry_failed_files(
        subtitle_import_job_id, payload.files
    )


@router.post(
    "/subtitle-imports/{subtitle_import_job_id}/rerun",
    response_model=SubtitleImportTriggerResponse,
)
def rerun_subtitle_import_job(subtitle_import_job_id: int):
    return SubtitleImportJobService.rerun_job(subtitle_import_job_id)


@router.delete(
    "/subtitle-imports/{subtitle_import_job_id}/failed-files",
    response_model=SubtitleImportJobResource,
)
def delete_subtitle_import_failed_file(
    subtitle_import_job_id: int,
    payload: DeleteFailedFileRequest,
):
    return SubtitleImportJobService.delete_failed_file(
        subtitle_import_job_id, payload.path
    )


@router.post(
    "/subtitle-imports/{subtitle_import_job_id}/failed-files/rename",
    response_model=SubtitleImportJobResource,
)
def rename_subtitle_import_failed_file(
    subtitle_import_job_id: int,
    payload: RenameFailedFileRequest,
):
    return SubtitleImportJobService.rename_failed_file(
        subtitle_import_job_id, payload.path, payload.new_name
    )
