from typing import List

from fastapi import APIRouter, Depends, Query, Response, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.videos.items import (
    VideoItemCreateRequest,
    VideoItemDetailResource,
    VideoItemListItemResource,
    VideoItemUpdateRequest,
)
from src.service.videos import VideoItemService

router = APIRouter(
    prefix="/videos",
    tags=["videos"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=PageResponse[VideoItemListItemResource])
def list_videos(
    query: str | None = Query(default=None),
    tag_id: List[int] | None = Query(default=None),
    person_id: List[int] | None = Query(default=None),
    sort: str | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
):
    return VideoItemService.list_videos(
        query=query,
        tag_ids=tag_id,
        person_ids=person_id,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=VideoItemDetailResource, status_code=status.HTTP_201_CREATED)
def create_video(payload: VideoItemCreateRequest):
    return VideoItemService.create_video(payload)


@router.get("/{video_id}", response_model=VideoItemDetailResource)
def get_video(video_id: int):
    return VideoItemService.get_video_detail(video_id)


@router.patch("/{video_id}", response_model=VideoItemDetailResource)
def update_video(video_id: int, payload: VideoItemUpdateRequest):
    return VideoItemService.update_video(video_id, payload)


@router.delete("/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_video(video_id: int):
    VideoItemService.delete_video(video_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
