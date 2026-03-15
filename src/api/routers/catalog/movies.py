import json
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse

from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.movies import (
    MovieCollectionType,
    MovieDetailResource,
    MovieJavdbSearchRequest,
    MovieListItemResource,
    MovieListStatus,
    MovieNumberParseRequest,
    MovieNumberParseResponse,
)
from src.schema.common.pagination import PageResponse
from src.service.catalog import MovieService

router = APIRouter(
    prefix="/movies",
    tags=["movies"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


def _to_sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("", response_model=PageResponse[MovieListItemResource])
def list_movies(
    actor_id: Optional[int] = None,
    status: MovieListStatus = MovieListStatus.ALL,
    collection_type: MovieCollectionType = MovieCollectionType.ALL,
    sort: Optional[str] = Query(default=None),
    page: int = 1,
    page_size: int = 20,
):
    return MovieService.list_movies(
        actor_id=actor_id,
        status=status,
        collection_type=collection_type,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@router.get("/latest", response_model=PageResponse[MovieListItemResource])
def list_latest_movies(page: int = 1, page_size: int = 20):
    return MovieService.list_latest_movies(page=page, page_size=page_size)


@router.get("/subscribed-actors/latest", response_model=PageResponse[MovieListItemResource])
def list_subscribed_actor_latest_movies(page: int = 1, page_size: int = 20):
    return MovieService.list_subscribed_actor_latest_movies(page=page, page_size=page_size)


@router.post("/search/parse-number", response_model=MovieNumberParseResponse)
def parse_movie_number(payload: MovieNumberParseRequest):
    return MovieService.parse_movie_number_query(payload.query)


@router.get("/search/local", response_model=List[MovieListItemResource])
def search_local_movies(movie_number: str = Query(..., min_length=1)):
    return MovieService.search_local_movies(movie_number=movie_number)


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


@router.get("/{movie_number}", response_model=MovieDetailResource)
def get_movie_detail(movie_number: str):
    return MovieService.get_movie_detail(movie_number)


@router.put("/{movie_number}/subscription", status_code=status.HTTP_204_NO_CONTENT)
def subscribe_movie(movie_number: str):
    MovieService.set_subscription(movie_number, True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{movie_number}/subscription", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe_movie(movie_number: str, delete_media: bool = False):
    MovieService.unsubscribe_movie(movie_number, delete_media=delete_media)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
