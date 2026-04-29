from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.discovery import HotReviewListItemResource
from src.service.discovery import HotReviewCatalogService

router = APIRouter(
    prefix="/hot-reviews",
    tags=["hot-reviews"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=PageResponse[HotReviewListItemResource])
def list_hot_reviews(
    period: str = Query(default=HotReviewCatalogService.DEFAULT_PERIOD, min_length=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1),
):
    return HotReviewCatalogService.list_items(
        period=period,
        page=page,
        page_size=page_size,
    )
