from fastapi import APIRouter, Depends, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.transfers.media_transfer import (
    MediaStorageTransferAcceptedResponse,
    MediaStorageTransferCandidatesRequest,
    MediaStorageTransferCandidatesResponse,
    MediaStorageTransferRequest,
)
from src.service.transfers.shared.media_transfer_task_service import (
    MediaTransferTaskService,
)

router = APIRouter(
    tags=["media-transfer"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post(
    "/media-transfers/candidates",
    response_model=MediaStorageTransferCandidatesResponse,
)
def list_media_transfer_candidates(payload: MediaStorageTransferCandidatesRequest):
    return MediaTransferTaskService.list_candidates(payload)


@router.post(
    "/media-transfers",
    response_model=MediaStorageTransferAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_media_transfer(payload: MediaStorageTransferRequest):
    return MediaTransferTaskService.enqueue(payload)
