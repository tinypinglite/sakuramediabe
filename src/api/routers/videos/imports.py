from fastapi import APIRouter, Depends, Query, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.transfers.media_import import (
    DeleteFailedFileRequest,
    RenameFailedFileRequest,
    RetryFailedFilesRequest,
)
from src.schema.videos.imports import (
    VideoImportJobListItemResource,
    VideoImportJobResource,
    VideoImportRequest,
    VideoImportTriggerResponse,
)
from src.service.videos import VideoImportJobService

router = APIRouter(
    prefix="/video-imports",
    tags=["video-imports"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post("", response_model=VideoImportTriggerResponse, status_code=status.HTTP_202_ACCEPTED)
def trigger_video_import(payload: VideoImportRequest):
    # 异步触发：把视频目录/单文件搬入媒体库并登记，进度经 /system/events/stream 或本作业详情查询。
    return VideoImportJobService.trigger_directory_import(
        payload.library_id,
        payload.source_path,
        transfer_mode=payload.transfer_mode,
        collection_id=payload.collection_id,
    )


@router.get("", response_model=PageResponse[VideoImportJobListItemResource])
def list_video_import_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return VideoImportJobService.list_jobs(page=page, page_size=page_size)


@router.get("/{video_import_job_id}", response_model=VideoImportJobResource)
def get_video_import_job(video_import_job_id: int):
    return VideoImportJobService.get_job(video_import_job_id)


@router.post("/{video_import_job_id}/retry", response_model=VideoImportTriggerResponse)
def retry_video_import_failed_files(video_import_job_id: int, payload: RetryFailedFilesRequest):
    return VideoImportJobService.retry_failed_files(video_import_job_id, payload.files)


@router.delete("/{video_import_job_id}/failed-files", response_model=VideoImportJobResource)
def delete_video_import_failed_file(video_import_job_id: int, payload: DeleteFailedFileRequest):
    return VideoImportJobService.delete_failed_file(video_import_job_id, payload.path)


@router.post(
    "/{video_import_job_id}/failed-files/rename",
    response_model=VideoImportJobResource,
)
def rename_video_import_failed_file(video_import_job_id: int, payload: RenameFailedFileRequest):
    return VideoImportJobService.rename_failed_file(
        video_import_job_id,
        payload.path,
        payload.new_name,
    )
