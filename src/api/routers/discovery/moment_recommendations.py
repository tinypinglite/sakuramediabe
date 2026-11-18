from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.discovery import MomentRecommendationPageResource
from src.service.discovery import MomentRecommendationService

router = APIRouter(
    prefix="/moment-recommendations",
    tags=["moment-recommendations"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=MomentRecommendationPageResource)
def list_moment_recommendations(
    page: int = Query(default=1),
    page_size: int = Query(default=20),
):
    return MomentRecommendationService.list_items(page=page, page_size=page_size)
