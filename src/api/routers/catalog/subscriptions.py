from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.subscriptions import (
    MovieSubscriptionListItemResource,
    MovieSubscriptionSearchResetRequest,
    MovieSubscriptionSearchResetResponse,
    MovieSubscriptionSort,
    MovieSubscriptionStatus,
    MovieSubscriptionStatusCountsResource,
)
from src.schema.common.pagination import PageResponse
from src.service.catalog import MovieSubscriptionService

# 独立顶层资源而非挂在 /movies 下：避免与 /movies/{movie_number} 抢路由，也不用依赖 router
# 注册顺序来保证匹配优先级。
router = APIRouter(
    prefix="/movie-subscriptions",
    tags=["movie-subscriptions"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=PageResponse[MovieSubscriptionListItemResource])
def list_movie_subscriptions(
    page: int = 1,
    page_size: int = 20,
    status: MovieSubscriptionStatus = MovieSubscriptionStatus.ALL,
    sort: MovieSubscriptionSort = MovieSubscriptionSort.SUBSCRIBED_AT_DESC,
    search: str | None = Query(default=None),
):
    return MovieSubscriptionService.list_subscriptions(
        page=page,
        page_size=page_size,
        status=status,
        sort=sort,
        search=search,
    )


@router.get("/status-counts", response_model=MovieSubscriptionStatusCountsResource)
def count_movie_subscriptions_by_status():
    return MovieSubscriptionService.count_by_status()


# 批量取消订阅不在本域：不删文件走 POST /movies/unsubscriptions，要删媒体文件走
# DELETE /media/{media_id}，两者都已存在，不在这里平行造一套。
@router.post("/search-resets", response_model=MovieSubscriptionSearchResetResponse)
def reset_movie_subscription_search(payload: MovieSubscriptionSearchResetRequest):
    return MovieSubscriptionService.reset_search_state(
        movie_numbers=payload.movie_numbers,
        reset_all_exhausted=payload.reset_all_exhausted,
    )
