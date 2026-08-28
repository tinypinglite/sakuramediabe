from fastapi import APIRouter, Depends, status

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.status import (
    ImageSearchResetResource,
    StatusImageSearchResource,
    StatusMetadataProviderTestResource,
    StatusResource,
)
from src.service.discovery.image_search_reset_service import ImageSearchResetService
from src.service.system.status_service import StatusService

router = APIRouter(
    tags=["status"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("/status", response_model=StatusResource)
def get_status():
    return StatusService.get_status()


@router.get("/status/image-search", response_model=StatusImageSearchResource)
def get_image_search_status():
    return StatusService.get_image_search_status()


@router.post(
    "/image-search/reset",
    response_model=ImageSearchResetResource,
    status_code=status.HTTP_202_ACCEPTED,
)
def reset_image_search():
    return ImageSearchResetService.reset()


@router.get(
    "/status/metadata-providers/{provider}/test",
    response_model=StatusMetadataProviderTestResource,
)
def test_metadata_provider(provider: str):
    normalized_provider = provider.strip().lower()
    if normalized_provider not in {"javdb"}:
        raise ApiError(
            422,
            "invalid_metadata_provider",
            "Metadata provider must be javdb",
            {"provider": provider},
        )
    return StatusService.test_metadata_provider(normalized_provider)
