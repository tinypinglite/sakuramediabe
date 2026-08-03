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
from src.service.transfers.cloud115.importer.job_service import Cloud115ImportJobService
from src.service.transfers.imports.browse_service import FilesystemBrowseService
from src.service.transfers.imports.job_service import (
    MediaImportJobService,
    import_job_service_for,
)

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
    # 按请求形状分派：source_cid → cloud115 导入；source_path → 本地目录导入。
    # schema 已保证两者恰好其一；库 backend 与请求形状不匹配时由对应 service 报 422。
    if payload.source_cid is not None:
        return Cloud115ImportJobService.trigger_cloud115_import(
            payload.library_id,
            payload.source_cid,
            transfer_mode=payload.transfer_mode or "cleanup-source",
        )
    return MediaImportJobService.trigger_directory_import(
        payload.library_id,
        payload.source_path,
        transfer_mode=payload.transfer_mode or "auto",
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
    return import_job_service_for(import_job_id).retry_failed_files(import_job_id, payload.files)



@router.post("/import-jobs/{import_job_id}/rerun", response_model=ImportJobTriggerResponse)
def rerun_import_job(import_job_id: int):
    """整作业重跑：不论上次成败按原源整目录重扫（completed 零产出的恢复通路）。"""
    return import_job_service_for(import_job_id).rerun_job(import_job_id)


@router.delete("/import-jobs/{import_job_id}/failed-files", response_model=ImportJobResource)
def delete_import_job_failed_file(import_job_id: int, payload: DeleteFailedFileRequest):
    return import_job_service_for(import_job_id).delete_failed_file(import_job_id, payload.path)


@router.post(
    "/import-jobs/{import_job_id}/failed-files/rename",
    response_model=ImportJobResource,
)
def rename_import_job_failed_file(import_job_id: int, payload: RenameFailedFileRequest):
    return import_job_service_for(import_job_id).rename_failed_file(
        import_job_id,
        payload.path,
        payload.new_name,
    )
