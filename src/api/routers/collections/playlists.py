
from fastapi import APIRouter, Depends, Query, Response, status

from src.api.routers._utils import (
    parse_csv_enums_allow_empty,
    parse_csv_positive_ints_allow_empty,
    parse_csv_texts_allow_empty,
)
from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.movies import MovieListStatus, MovieWatchStatus, TagMatchMode
from src.schema.collections.playlists import (
    PlaylistCreateRequest,
    PlaylistMovieBatchAddRequest,
    PlaylistMovieBatchAddResponse,
    PlaylistMovieListItemResource,
    PlaylistResolutionOption,
    PlaylistResource,
    PlaylistUpdateRequest,
)
from src.schema.common.pagination import PageResponse
from src.service.collections import PlaylistService

router = APIRouter(
    prefix="/playlists",
    tags=["playlists"],
    dependencies=[Depends(db_deps)],
)


@router.get("", response_model=list[PlaylistResource])
def list_playlists(include_system: bool = Query(default=True), current_user=Depends(get_current_user)):
    return PlaylistService.list_playlists(include_system=include_system)


@router.post("", response_model=PlaylistResource, status_code=status.HTTP_201_CREATED)
def create_playlist(payload: PlaylistCreateRequest, current_user=Depends(get_current_user)):
    return PlaylistService.create_playlist(payload)


@router.get("/{playlist_id}", response_model=PlaylistResource)
def get_playlist(playlist_id: int, current_user=Depends(get_current_user)):
    return PlaylistService.get_playlist(playlist_id)


@router.patch("/{playlist_id}", response_model=PlaylistResource)
def update_playlist(
    playlist_id: int,
    payload: PlaylistUpdateRequest,
    current_user=Depends(get_current_user),
):
    return PlaylistService.update_playlist(playlist_id, payload)


@router.delete("/{playlist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_playlist(playlist_id: int, current_user=Depends(get_current_user)):
    PlaylistService.delete_playlist(playlist_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{playlist_id}/movies", response_model=PageResponse[PlaylistMovieListItemResource])
def list_playlist_movies(
    playlist_id: int,
    page: int = 1,
    page_size: int = 20,
    sort: str | None = Query(default=None),
    resolution: str | None = Query(default=None),
    resolutions: str | None = Query(default=None),
    watch_status: MovieWatchStatus | None = Query(default=None),
    watch_statuses: str | None = Query(default=None),
    watch_match: TagMatchMode | None = Query(default=None),
    statuses: str | None = Query(default=None),
    status_match: TagMatchMode | None = Query(default=None),
    year: str | None = Query(default=None),
    tag_ids: str | None = Query(default=None),
    tag_match: TagMatchMode | None = Query(default=None),
    media_tag_ids: str | None = Query(default=None),
    media_tag_match: TagMatchMode | None = Query(default=None),
    current_user=Depends(get_current_user),
):
    parsed_watch_statuses = parse_csv_enums_allow_empty(
        watch_statuses,
        MovieWatchStatus,
        "watch_statuses",
        error_code="invalid_playlist_filter",
    )
    if parsed_watch_statuses is None and watch_status is not None:
        parsed_watch_statuses = (
            []
            if watch_status == MovieWatchStatus.ALL
            else [watch_status]
        )
    return PlaylistService.list_playlist_movies(
        playlist_id=playlist_id,
        page=page,
        page_size=page_size,
        sort=sort,
        resolution=resolution,
        resolutions=parse_csv_texts_allow_empty(
            resolutions,
            "resolutions",
            error_code="invalid_playlist_filter",
        ),
        watch_statuses=parsed_watch_statuses,
        watch_match=watch_match,
        statuses=parse_csv_enums_allow_empty(
            statuses,
            MovieListStatus,
            "statuses",
            error_code="invalid_playlist_filter",
        ),
        status_match=status_match,
        years=parse_csv_positive_ints_allow_empty(
            year,
            "year",
            error_code="invalid_playlist_filter",
        ),
        tag_ids=parse_csv_positive_ints_allow_empty(
            tag_ids,
            "tag_ids",
            error_code="invalid_playlist_filter",
        ),
        tag_match=tag_match,
        media_tag_ids=parse_csv_positive_ints_allow_empty(
            media_tag_ids,
            "media_tag_ids",
            error_code="invalid_playlist_filter",
        ),
        media_tag_match=media_tag_match,
    )


@router.post(
    "/{playlist_id}/movies",
    response_model=PlaylistMovieBatchAddResponse,
)
def add_movies_to_playlist(
    playlist_id: int,
    payload: PlaylistMovieBatchAddRequest,
    current_user=Depends(get_current_user),
):
    return PlaylistService.add_movies_to_playlist(
        playlist_id,
        payload.movie_numbers,
    )


@router.get("/{playlist_id}/resolutions", response_model=list[PlaylistResolutionOption])
def list_playlist_resolutions(playlist_id: int, current_user=Depends(get_current_user)):
    return PlaylistService.list_playlist_resolutions(playlist_id)


@router.put("/{playlist_id}/movies/{movie_number}", status_code=status.HTTP_204_NO_CONTENT)
def add_movie_to_playlist(
    playlist_id: int,
    movie_number: str,
    current_user=Depends(get_current_user),
):
    PlaylistService.add_movie_to_playlist(playlist_id, movie_number)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{playlist_id}/movies/{movie_number}", status_code=status.HTTP_204_NO_CONTENT)
def remove_movie_from_playlist(
    playlist_id: int,
    movie_number: str,
    current_user=Depends(get_current_user),
):
    PlaylistService.remove_movie_from_playlist(playlist_id, movie_number)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
