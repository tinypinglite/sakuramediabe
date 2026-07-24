from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.routers._utils import (
    parse_csv_positive_ints,
    parse_optional_exact_text,
    sse_streaming_response,
    to_sse_event,
)
from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.movies import (
    MovieCollectionMarkRequest,
    MovieCollectionMarkResponse,
    MovieCollectionStatusResource,
    MovieCollectionType,
    MovieDetailResource,
    MovieJavdbSearchRequest,
    MovieListItemResource,
    MovieListStatus,
    MovieNumberParseRequest,
    MovieNumberParseResponse,
    MovieNumberSource,
    MovieReviewSort,
    MovieSeriesListRequest,
    MovieSpecialTagFilter,
    MovieSubscriptionBatchRequest,
    MovieSubscriptionBatchResponse,
    SimilarMovieListItemResource,
    TagMatchMode,
)
from src.schema.catalog.subtitles import MovieSubtitleListResource
from src.schema.common.pagination import PageResponse
from src.metadata._providers.models import JavdbMovieReviewResource
from src.service.catalog import (
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


@router.get("", response_model=PageResponse[MovieListItemResource])
def list_movies(
    actor_id: Optional[int] = None,
    tag_ids: str | None = Query(default=None),
    tag_match: TagMatchMode = Query(default=TagMatchMode.OR),
    year: int | None = Query(default=None, ge=1),
    status: MovieListStatus = MovieListStatus.ALL,
    collection_type: MovieCollectionType = MovieCollectionType.ALL,
    special_tag: MovieSpecialTagFilter | None = None,
    number_source: MovieNumberSource = MovieNumberSource.ALL,
    sort: Optional[str] = Query(default=None),
    director_name: str | None = Query(default=None),
    maker_name: str | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
):
    return MovieService.list_movies(
        actor_id=actor_id,
        tag_ids=parse_csv_positive_ints(tag_ids, "tag_ids", error_code="invalid_movie_filter"),
        tag_match=tag_match,
        year=year,
        status=status,
        collection_type=collection_type,
        special_tag=special_tag,
        number_source=number_source,
        sort=sort,
        director_name=parse_optional_exact_text(
            director_name, "director_name", error_code="invalid_movie_filter"
        ),
        maker_name=parse_optional_exact_text(
            maker_name, "maker_name", error_code="invalid_movie_filter"
        ),
        page=page,
        page_size=page_size,
    )


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


@router.post("/series/{series_id}/javdb/import/stream")
def import_series_movies_from_javdb_stream(series_id: int):
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


@router.get("/search/local", response_model=List[MovieListItemResource])
def search_local_movies(movie_number: str = Query(..., min_length=1)):
    return MovieService.search_local_movies(movie_number=movie_number)


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


@router.get("/{movie_number}/reviews", response_model=List[JavdbMovieReviewResource])
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


@router.get("/{movie_number}/similar", response_model=List[SimilarMovieListItemResource])
def list_similar_movies(
    movie_number: str,
    limit: int = Query(default=20, ge=0, le=100),
):
    return MovieRecommendationService().list_similar_resources(
        movie_number=movie_number,
        limit=limit,
    )


@router.post("/search/javdb/stream")
def search_javdb_movies_stream(payload: MovieJavdbSearchRequest):
    def stream():
        for event, event_payload in MovieMetadataRefreshService.stream_search_and_upsert_movie_from_javdb(
            payload.movie_number
        ):
            yield to_sse_event(event, event_payload)

    return sse_streaming_response(stream())


@router.post("/{movie_number}/metadata-refresh", response_model=MovieDetailResource)
def refresh_movie_metadata(movie_number: str):
    return MovieMetadataRefreshService.refresh_movie_metadata(movie_number)


@router.post("/{movie_number}/desc-translation", response_model=MovieDetailResource)
def translate_movie_desc(movie_number: str):
    return MovieTaskService.translate_movie_desc(movie_number)


@router.post("/{movie_number}/interaction-sync", response_model=MovieDetailResource)
def sync_movie_interactions(movie_number: str):
    return MovieTaskService.sync_movie_interactions(movie_number)


@router.post("/{movie_number}/heat-recompute", response_model=MovieDetailResource)
def recompute_movie_heat(movie_number: str):
    return MovieTaskService.recompute_movie_heat(movie_number)


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
