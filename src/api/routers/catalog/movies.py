import json
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse

from src.api.exception.errors import ApiError
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
    MovieReviewSort,
    MovieSeriesListRequest,
    MovieSpecialTagFilter,
    SimilarMovieListItemResource,
)
from src.schema.catalog.subtitles import MovieSubtitleListResource
from src.schema.common.pagination import PageResponse
from sakuramedia_metadata_providers.models import JavdbMovieReviewResource
from src.service.catalog import MovieService, MovieSubtitleService
from src.service.discovery import MovieRecommendationService

router = APIRouter(
    prefix="/movies",
    tags=["movies"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


def _to_sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _parse_csv_positive_ints(raw: str | None, field_name: str) -> list[int] | None:
    if raw is None:
        return None

    # 数组筛选参数必须显式传入正整数，避免把空串或脏值静默吞掉。
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part for part in parts):
        raise ApiError(
            422,
            "invalid_movie_filter",
            "Invalid filter value",
            {field_name: raw},
        )

    try:
        values = [int(part) for part in parts]
    except ValueError as exc:
        raise ApiError(
            422,
            "invalid_movie_filter",
            "Invalid filter value",
            {field_name: raw},
        ) from exc

    if any(value <= 0 for value in values):
        raise ApiError(
            422,
            "invalid_movie_filter",
            "Invalid filter value",
            {field_name: raw},
        )
    return values


def _parse_optional_exact_text(raw: str | None, field_name: str) -> str | None:
    if raw is None:
        return None

    normalized = raw.strip()
    if not normalized:
        raise ApiError(
            422,
            "invalid_movie_filter",
            "Invalid filter value",
            {field_name: raw},
        )
    return normalized


@router.get("", response_model=PageResponse[MovieListItemResource])
def list_movies(
    actor_id: Optional[int] = None,
    tag_ids: str | None = Query(default=None),
    year: int | None = Query(default=None, ge=1),
    status: MovieListStatus = MovieListStatus.ALL,
    collection_type: MovieCollectionType = MovieCollectionType.ALL,
    special_tag: MovieSpecialTagFilter | None = None,
    sort: Optional[str] = Query(default=None),
    director_name: str | None = Query(default=None),
    maker_name: str | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
):
    return MovieService.list_movies(
        actor_id=actor_id,
        tag_ids=_parse_csv_positive_ints(tag_ids, "tag_ids"),
        year=year,
        status=status,
        collection_type=collection_type,
        special_tag=special_tag,
        sort=sort,
        director_name=_parse_optional_exact_text(director_name, "director_name"),
        maker_name=_parse_optional_exact_text(maker_name, "maker_name"),
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
        for event, event_payload in MovieService.stream_import_series_movies_from_javdb(series_id):
            yield _to_sse_event(event, event_payload)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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


@router.get("/{movie_number}/thumbnails/missav/stream")
def stream_missav_movie_thumbnails(movie_number: str, refresh: bool = False):
    def stream():
        for event, event_payload in MovieService.stream_missav_thumbnails(
            movie_number=movie_number,
            refresh=refresh,
        ):
            yield _to_sse_event(event, event_payload)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/search/javdb/stream")
def search_javdb_movies_stream(payload: MovieJavdbSearchRequest):
    def stream():
        for event, event_payload in MovieService.stream_search_and_upsert_movie_from_javdb(
            payload.movie_number
        ):
            yield _to_sse_event(event, event_payload)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{movie_number}/metadata-refresh", response_model=MovieDetailResource)
def refresh_movie_metadata(movie_number: str):
    return MovieService.refresh_movie_metadata(movie_number)


@router.post("/{movie_number}/desc-translation", response_model=MovieDetailResource)
def translate_movie_desc(movie_number: str):
    return MovieService.translate_movie_desc(movie_number)


@router.post("/{movie_number}/interaction-sync", response_model=MovieDetailResource)
def sync_movie_interactions(movie_number: str):
    return MovieService.sync_movie_interactions(movie_number)


@router.post("/{movie_number}/heat-recompute", response_model=MovieDetailResource)
def recompute_movie_heat(movie_number: str):
    return MovieService.recompute_movie_heat(movie_number)


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
