from fastapi import APIRouter, Depends

from src.api.routers.deps import db_deps, get_current_user
from src.schema.transfers.download_selection import (
    DownloadSelectionPolicyResource,
    DownloadSelectionPolicyUpdateRequest,
    DownloadSelectionPreviewRequest,
    DownloadSelectionPreviewResponse,
)
from src.service.transfers.downloads.selection.policy_service import (
    DownloadSelectionPolicyService,
)

router = APIRouter(
    tags=["download-selection"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get(
    "/download-selection-policy",
    response_model=DownloadSelectionPolicyResource,
)
def get_download_selection_policy():
    return DownloadSelectionPolicyService.get_resource()


@router.put(
    "/download-selection-policy",
    response_model=DownloadSelectionPolicyResource,
)
def update_download_selection_policy(
    payload: DownloadSelectionPolicyUpdateRequest,
):
    return DownloadSelectionPolicyService.update(payload)


@router.post(
    "/download-selection-policy/preview",
    response_model=DownloadSelectionPreviewResponse,
)
def preview_download_selection_policy(
    payload: DownloadSelectionPreviewRequest,
):
    return DownloadSelectionPolicyService.preview(payload)
