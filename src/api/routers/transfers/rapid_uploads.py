from fastapi import APIRouter, Depends, Query, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.transfers.rapid_upload import (
    MediaRapidUploadBatchListItemResource,
    MediaRapidUploadBatchResource,
    MediaRapidUploadCreateRequest,
    MediaRapidUploadTriggerResponse,
)
from src.service.transfers import MediaRapidUploadService

router = APIRouter(
    prefix="/media/rapid-uploads",
    tags=["transfers"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post(
    "",
    response_model=MediaRapidUploadTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_media_rapid_upload(payload: MediaRapidUploadCreateRequest):
    return MediaRapidUploadService.trigger_batch(
        media_ids=payload.media_ids,
        target_library_id=payload.target_library_id,
    )


@router.get("", response_model=PageResponse[MediaRapidUploadBatchListItemResource])
def list_media_rapid_uploads(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return MediaRapidUploadService.list_batches(page=page, page_size=page_size)


@router.get("/{batch_id}", response_model=MediaRapidUploadBatchResource)
def get_media_rapid_upload(batch_id: int):
    return MediaRapidUploadService.get_batch(batch_id)


@router.post(
    "/{batch_id}/retry",
    response_model=MediaRapidUploadTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_media_rapid_upload(batch_id: int):
    return MediaRapidUploadService.retry_batch(batch_id)
