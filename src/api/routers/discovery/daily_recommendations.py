from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.discovery import DailyRecommendationMovieResource
from src.service.discovery import DailyRecommendationService

router = APIRouter(
    prefix="/daily-recommendations",
    tags=["daily-recommendations"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=PageResponse[DailyRecommendationMovieResource])
def list_daily_recommendations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    return DailyRecommendationService.list_items(page=page, page_size=page_size)
