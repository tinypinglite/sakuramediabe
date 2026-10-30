
from fastapi import APIRouter, Depends, Query, Response, status

from src.api.routers._utils import (
    parse_csv_positive_ints,
    parse_optional_exact_text,
    sse_streaming_response,
    to_sse_event,
)
from src.api.routers.deps import db_deps, get_current_user
from src.metadata._providers.models import JavdbMovieReviewResource
from src.schema.catalog.movies import (
    MovieBlacklistBatchRequest,
    MovieCollectionMarkRequest,
    MovieCollectionMarkResponse,
    MovieCollectionStatusResource,
    MovieCollectionType,
    MovieDetailResource,
    MovieInteractionFilterOptionsResource,
    MovieListItemResource,
    MovieListItemWithRatingResource,
    MovieListStatus,
    MovieMergedPlaybackResource,
    MovieMetadataImportRequest,
    MovieNumberParseRequest,
    MovieNumberParseResponse,
    MovieNumberSource,
    MovieReviewSort,
    MovieSeriesListRequest,
    MovieSubscriptionBatchRequest,
    MovieSubscriptionBatchResponse,
    SimilarMovieListItemResource,
    TagMatchMode,
)
from src.schema.catalog.subtitles import MovieSubtitleListResource
from src.schema.common.pagination import PageResponse
from src.schema.system.jobs import ManualJobTriggerResponse
from src.service.catalog import (
    MovieActorService,
    MovieMetadataRefreshService,
    MovieService,
    MovieSubtitleService,
    MovieTaskService,
)
from src.service.discovery import MovieRecommendationService

router = APIRouter(
    prefix="/movies",
    tags=["movies"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=PageResponse[MovieListItemWithRatingResource])
def list_movies(
    actor_id: int | None = None,
    actor_age_min: int | None = Query(default=None, ge=0),
    actor_age_max: int | None = Query(default=None, ge=0),
    tag_ids: str | None = Query(default=None),
    tag_match: TagMatchMode = Query(default=TagMatchMode.OR),
    media_tag_ids: str | None = Query(default=None),
    media_tag_match: TagMatchMode = Query(default=TagMatchMode.OR),
    year: str | None = Query(default=None),
    status: MovieListStatus = MovieListStatus.ALL,
    collection_type: MovieCollectionType = MovieCollectionType.ALL,
    number_source: MovieNumberSource = MovieNumberSource.ALL,
    sort: str | None = Query(default=None),
    director_name: str | None = Query(default=None),
    maker_name: str | None = Query(default=None),
    heat_min: int | None = Query(default=None, ge=0),
    heat_max: int | None = Query(default=None, ge=0),
    score_min: float | None = Query(default=None, ge=0),
    score_max: float | None = Query(default=None, ge=0),
    score_number_min: int | None = Query(default=None, ge=0),
    score_number_max: int | None = Query(default=None, ge=0),
    watched_count_min: int | None = Query(default=None, ge=0),
    watched_count_max: int | None = Query(default=None, ge=0),
    want_watch_count_min: int | None = Query(default=None, ge=0),
    want_watch_count_max: int | None = Query(default=None, ge=0),
    comment_count_min: int | None = Query(default=None, ge=0),
    comment_count_max: int | None = Query(default=None, ge=0),
    resolution: str | None = Query(default=None),
    blacklisted: bool = False,
    query: str | None = Query(default=None),
    rated: bool = Query(default=False),
    page: int = 1,
    page_size: int = 20,
):
    return MovieService.list_movies(
        actor_id=actor_id,
        actor_age_min=actor_age_min,
        actor_age_max=actor_age_max,
        tag_ids=parse_csv_positive_ints(tag_ids, "tag_ids", error_code="invalid_movie_filter"),
        tag_match=tag_match,
        media_tag_ids=parse_csv_positive_ints(
            media_tag_ids, "media_tag_ids", error_code="invalid_movie_filter"
        ),
        media_tag_match=media_tag_match,
        years=parse_csv_positive_ints(year, "year", error_code="invalid_movie_filter"),
        status=status,
        collection_type=collection_type,
        number_source=number_source,
        sort=sort,
        director_name=parse_optional_exact_text(
            director_name, "director_name", error_code="invalid_movie_filter"
        ),
        maker_name=parse_optional_exact_text(
            maker_name, "maker_name", error_code="invalid_movie_filter"
        ),
        heat_min=heat_min,
        heat_max=heat_max,
        score_min=score_min,
        score_max=score_max,
        score_number_min=score_number_min,
        score_number_max=score_number_max,
        watched_count_min=watched_count_min,
        watched_count_max=watched_count_max,
        want_watch_count_min=want_watch_count_min,
        want_watch_count_max=want_watch_count_max,
        comment_count_min=comment_count_min,
        comment_count_max=comment_count_max,
        resolution=resolution,
        blacklisted=blacklisted,
        rated=rated,
        query=query,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/interaction-filter-options",
    response_model=MovieInteractionFilterOptionsResource,
)
def get_movie_interaction_filter_options():
    return MovieService.get_interaction_filter_options()


@router.get("/latest", response_model=PageResponse[MovieListItemResource])
def list_latest_movies(page: int = 1, page_size: int = 20):
    return MovieService.list_latest_movies(page=page, page_size=page_size)


@router.post("/by-series", response_model=PageResponse[MovieListItemResource])
def list_movies_by_series(payload: MovieSeriesListRequest):
    return MovieService.list_movies_by_series(
        series_id=payload.series_id,
        sort=payload.sort,
        page=payload.page,
        page_size=payload.page_size,
    )


@router.post("/series/{series_id}/metadata-imports")
def create_series_metadata_import(series_id: int):
    def stream():
        for event, event_payload in MovieMetadataRefreshService.stream_import_series_movies_from_javdb(series_id):
            yield to_sse_event(event, event_payload)

    return sse_streaming_response(stream())


@router.get("/subscribed-actors/latest", response_model=PageResponse[MovieListItemResource])
def list_subscribed_actor_latest_movies(page: int = 1, page_size: int = 20):
    return MovieService.list_subscribed_actor_latest_movies(page=page, page_size=page_size)


@router.post("/search/parse-number", response_model=MovieNumberParseResponse)
def parse_movie_number(payload: MovieNumberParseRequest):
    return MovieService.parse_movie_number_query(payload.query)


@router.get("/{movie_number}/collection-status", response_model=MovieCollectionStatusResource)
def get_movie_collection_status(movie_number: str):
    return MovieService.get_movie_collection_status(movie_number)


@router.patch("/collection-type", response_model=MovieCollectionMarkResponse)
def mark_movie_collection_type(payload: MovieCollectionMarkRequest):
    return MovieService.mark_movie_collection_type(
        movie_numbers=payload.movie_numbers,
        collection_type=payload.collection_type,
    )


@router.post("/subscriptions", response_model=MovieSubscriptionBatchResponse)
def batch_subscribe_movies(payload: MovieSubscriptionBatchRequest):
    return MovieService.batch_set_subscription(payload.movie_numbers)


@router.post("/unsubscriptions", response_model=MovieSubscriptionBatchResponse)
def batch_unsubscribe_movies(payload: MovieSubscriptionBatchRequest):
    return MovieService.batch_unsubscribe_movies(payload.movie_numbers)


@router.put("/blacklist", status_code=status.HTTP_204_NO_CONTENT)
def blacklist_movies(payload: MovieBlacklistBatchRequest):
    MovieService.set_blacklisted(payload, blacklisted=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/blacklist", status_code=status.HTTP_204_NO_CONTENT)
def unblacklist_movies(payload: MovieBlacklistBatchRequest):
    MovieService.set_blacklisted(payload, blacklisted=False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{movie_number}/reviews", response_model=list[JavdbMovieReviewResource])
def get_movie_reviews(
    movie_number: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1),
    sort: MovieReviewSort = MovieReviewSort.RECENTLY,
):
    return MovieService.get_movie_reviews(
        movie_number=movie_number,
        page=page,
        page_size=page_size,
        sort=sort,
    )


@router.get("/{movie_number}/subtitles", response_model=MovieSubtitleListResource)
def get_movie_subtitles(movie_number: str):
    return MovieSubtitleService.get_movie_subtitles(movie_number)


@router.get("/{movie_number}/similar", response_model=list[SimilarMovieListItemResource])
def list_similar_movies(
    movie_number: str,
    limit: int = Query(default=20, ge=0, le=100),
):
    return MovieRecommendationService().list_similar_resources(
        movie_number=movie_number,
        limit=limit,
    )


@router.post("/metadata-imports")
def create_movie_metadata_import(payload: MovieMetadataImportRequest):
    def stream():
        for event, event_payload in MovieMetadataRefreshService.stream_search_and_upsert_movie_from_javdb(
            payload.movie_number
        ):
            yield to_sse_event(event, event_payload)

    return sse_streaming_response(stream())


@router.post("/{movie_number}/metadata-refresh", response_model=MovieDetailResource)
def refresh_movie_metadata(
    movie_number: str,
    refresh_images: bool = Query(
        default=False,
        description="强制整批替换封面/剧照/头像；默认只补缺图。",
    ),
):
    return MovieMetadataRefreshService.refresh_movie_metadata(
        movie_number, replace_images=refresh_images
    )


# 影片单片翻译端点已删除；互动同步由常规定时任务负责。


@router.post(
    "/{movie_number}/heat-recompute",
    response_model=ManualJobTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def recompute_movie_heat(movie_number: str):
    return MovieTaskService.recompute_movie_heat(movie_number)


@router.put(
    "/{movie_number}/actors/{actor_id}", status_code=status.HTTP_204_NO_CONTENT
)
def add_movie_actor(movie_number: str, actor_id: int):
    """幂等加入演员；首次编辑后影片演员表转为人工管理。"""
    MovieActorService.add_actor(movie_number, actor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{movie_number}/actors/{actor_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_movie_actor(movie_number: str, actor_id: int):
    """幂等移除演员；首次编辑后影片演员表转为人工管理。"""
    MovieActorService.remove_actor(movie_number, actor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{movie_number}/actors/reset", status_code=status.HTTP_204_NO_CONTENT
)
def reset_movie_actor_ownership(movie_number: str):
    """恢复演员表自动管理；下一次元数据刷新会重新同步远端演员。"""
    MovieActorService.reset_ownership(movie_number)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{movie_number}/merged-playback", response_model=MovieMergedPlaybackResource)
def get_merged_playback(movie_number: str, library_id: int = Query(..., ge=1)):
    return MovieService.get_merged_playback(movie_number, library_id)


@router.get("/{movie_number}", response_model=MovieDetailResource)
def get_movie_detail(movie_number: str):
    return MovieService.get_movie_detail(movie_number)


@router.put("/{movie_number}/subscription", status_code=status.HTTP_204_NO_CONTENT)
def subscribe_movie(movie_number: str):
    MovieService.set_subscription(movie_number, True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{movie_number}/subscription", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe_movie(movie_number: str):
    MovieService.unsubscribe_movie(movie_number)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
