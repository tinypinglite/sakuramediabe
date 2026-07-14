from fastapi import APIRouter, Depends

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.status import (
    StatusCloud115CookiesResource,
    StatusImageSearchResource,
    StatusMetadataProviderTestResource,
    StatusResource,
)
from src.service.system.status_service import StatusService

router = APIRouter(
    tags=["status"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("/status", response_model=StatusResource)
def get_status():
    return StatusService.get_status()


@router.get(
    "/status/media-libraries/cloud115",
    response_model=StatusCloud115CookiesResource,
)
async def get_cloud115_cookies_status():
    """实时返回所有 115 媒体库的 cookies 三态探测结果。"""
    return await StatusService.get_cloud115_cookies_status()


@router.get("/status/image-search", response_model=StatusImageSearchResource)
def get_image_search_status():
    return StatusService.get_image_search_status()


@router.get(
    "/status/metadata-providers/{provider}/test",
    response_model=StatusMetadataProviderTestResource,
)
def test_metadata_provider(provider: str):
    normalized_provider = provider.strip().lower()
    if normalized_provider not in {"javdb", "dmm"}:
        raise ApiError(
            422,
            "invalid_metadata_provider",
            "Metadata provider must be javdb or dmm",
            {"provider": provider},
        )
    return StatusService.test_metadata_provider(normalized_provider)
