from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.discovery import HotActressReleaseMovieResource
from src.service.discovery import HotActressReleaseService

router = APIRouter(
    prefix="/hot-actress-releases",
    tags=["hot-actress-releases"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=PageResponse[HotActressReleaseMovieResource])
def list_hot_actress_releases(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return HotActressReleaseService.list_items(page=page, page_size=page_size)
