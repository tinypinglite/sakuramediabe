"""手动字幕目录导入接口。"""

from fastapi import APIRouter, Depends, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.subtitle_imports import SubtitleImportCreateRequest
from src.schema.system.jobs import ManualJobTriggerResponse
from src.service.catalog.subtitle_import_service import SubtitleImportService

router = APIRouter(
    tags=["subtitle-import"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post(
    "/subtitle-imports",
    response_model=ManualJobTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_subtitle_import(payload: SubtitleImportCreateRequest):
    return SubtitleImportService.trigger_directory_import(payload.source_path)
