from fastapi import APIRouter, Depends, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.videos.imports import VideoImportRequest, VideoImportResultResource
from src.service.videos import VideoImportService

router = APIRouter(
    prefix="/video-imports",
    tags=["video-imports"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post("", response_model=VideoImportResultResource, status_code=status.HTTP_201_CREATED)
def import_videos(payload: VideoImportRequest):
    # 就地索引指定目录或单文件为 VideoItem + Media，并按需关联标签/人物/合集。
    return VideoImportService().import_from_source(payload)
