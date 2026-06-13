from fastapi import APIRouter, Depends, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.videos.imports import (
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


@router.get("/{video_import_job_id}", response_model=VideoImportJobResource)
def get_video_import_job(video_import_job_id: int):
    return VideoImportJobService.get_job(video_import_job_id)
