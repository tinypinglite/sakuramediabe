from fastapi import APIRouter, Depends, Query, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.announcements import StatusAnnouncementsResource
from src.schema.system.status import (
    ImageSearchResetResource,
    StatusImageSearchResource,
    StatusInsightsResource,
    StatusResource,
    StatusWatchTrendRange,
    StatusWatchTrendResource,
)
from src.service.discovery.image_search_reset_service import ImageSearchResetService
from src.service.system.announcement_service import announcement_service
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


@router.get("/status/announcements", response_model=StatusAnnouncementsResource)
def get_announcements():
    return announcement_service.get_active()


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
