
from fastapi import APIRouter, Depends, Query, Response, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.videos.collections import (
    VideoCollectionCreateRequest,
    VideoCollectionItemAddRequest,
    VideoCollectionItemResource,
    VideoCollectionReorderRequest,
    VideoCollectionResource,
    VideoCollectionUpdateRequest,
)
from src.service.videos import VideoCollectionService

router = APIRouter(
    prefix="/video-collections",
    tags=["video-collections"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=list[VideoCollectionResource])
def list_collections():
    return VideoCollectionService.list_collections()


@router.post("", response_model=VideoCollectionResource, status_code=status.HTTP_201_CREATED)
def create_collection(payload: VideoCollectionCreateRequest):
    return VideoCollectionService.create_collection(payload)


@router.get("/{collection_id}", response_model=VideoCollectionResource)
def get_collection(collection_id: int):
    return VideoCollectionService.get_collection(collection_id)


@router.patch("/{collection_id}", response_model=VideoCollectionResource)
def update_collection(collection_id: int, payload: VideoCollectionUpdateRequest):
    return VideoCollectionService.update_collection(collection_id, payload)


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_collection(collection_id: int):
    VideoCollectionService.delete_collection(collection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{collection_id}/items", response_model=PageResponse[VideoCollectionItemResource])
def list_collection_items(
    collection_id: int,
    sort: str | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
    include_play_url: bool = False,
):
    return VideoCollectionService.list_collection_items(
        collection_id,
        sort=sort,
        page=page,
        page_size=page_size,
        include_play_url=include_play_url,
    )


@router.post("/{collection_id}/items", status_code=status.HTTP_204_NO_CONTENT)
def add_collection_item(collection_id: int, payload: VideoCollectionItemAddRequest):
    VideoCollectionService.add_item(collection_id, payload.video_item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{collection_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_collection_item(collection_id: int, item_id: int):
    VideoCollectionService.remove_item(collection_id, item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{collection_id}/items/reorder", response_model=list[VideoCollectionItemResource])
def reorder_collection_items(collection_id: int, payload: VideoCollectionReorderRequest):
    return VideoCollectionService.reorder_items(collection_id, payload.ordered_item_ids)
