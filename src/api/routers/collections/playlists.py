from typing import List

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.collections.playlists import (
    PlaylistCreateRequest,
    PlaylistMovieListItemResource,
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


@router.get("", response_model=List[PlaylistResource])
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
    current_user=Depends(get_current_user),
):
    return PlaylistService.list_playlist_movies(playlist_id=playlist_id, page=page, page_size=page_size)


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
