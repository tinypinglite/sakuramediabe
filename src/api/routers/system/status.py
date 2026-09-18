from fastapi import APIRouter, Depends, Query, status

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.status import (
    ImageSearchResetResource,
    StatusImageSearchResource,
    StatusInsightsResource,
    StatusMetadataProviderTestResource,
    StatusResource,
    StatusWatchTrendRange,
    StatusWatchTrendResource,
)
from src.service.discovery.image_search_reset_service import ImageSearchResetService
from src.service.system.optional_services import capabilities
from src.service.system.status_service import StatusService

router = APIRouter(
    tags=["status"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("/status/capabilities")
def get_capabilities():
    return capabilities()


@router.get("/status", response_model=StatusResource)
def get_status():
    return StatusService.get_status()


@router.get("/status/insights", response_model=StatusInsightsResource)
def get_status_insights():
    return StatusService.get_insights()


@router.get("/status/watch-trend", response_model=StatusWatchTrendResource)
def get_status_watch_trend(
    range: StatusWatchTrendRange = Query(default=StatusWatchTrendRange.LAST_30_DAYS),
):
    return StatusService.get_watch_trend(range)


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
