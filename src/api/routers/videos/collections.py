from typing import List

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import PlainTextResponse

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.common import verify_video_collection_playlist_signature
from src.schema.common.pagination import PageResponse
from src.schema.videos.collections import (
    VideoCollectionCreateRequest,
    VideoCollectionItemAddRequest,
    VideoCollectionItemResource,
    VideoCollectionReorderRequest,
    VideoCollectionResource,
    VideoCollectionThumbnailResource,
    VideoCollectionUpdateRequest,
)
from src.service.videos import VideoCollectionService

# 账号鉴权下沉到端点级，与 clip-collections 风格保持一致：m3u8 端点走签名访问、无账号 Cookie。
router = APIRouter(
    prefix="/video-collections",
    tags=["video-collections"],
    dependencies=[Depends(db_deps)],
)


@router.get("", response_model=List[VideoCollectionResource])
def list_collections(current_user=Depends(get_current_user)):
    return VideoCollectionService.list_collections()


@router.post("", response_model=VideoCollectionResource, status_code=status.HTTP_201_CREATED)
def create_collection(
    payload: VideoCollectionCreateRequest, current_user=Depends(get_current_user)
):
    return VideoCollectionService.create_collection(payload)


@router.get("/{collection_id}", response_model=VideoCollectionResource)
def get_collection(collection_id: int, current_user=Depends(get_current_user)):
    return VideoCollectionService.get_collection(collection_id)


@router.patch("/{collection_id}", response_model=VideoCollectionResource)
def update_collection(
    collection_id: int,
    payload: VideoCollectionUpdateRequest,
    current_user=Depends(get_current_user),
):
    return VideoCollectionService.update_collection(collection_id, payload)


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_collection(collection_id: int, current_user=Depends(get_current_user)):
    VideoCollectionService.delete_collection(collection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{collection_id}/items", response_model=PageResponse[VideoCollectionItemResource])
def list_collection_items(
    collection_id: int,
    sort: str | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
    include_play_url: bool = False,
    current_user=Depends(get_current_user),
):
    return VideoCollectionService.list_collection_items(
        collection_id,
        sort=sort,
        page=page,
        page_size=page_size,
        include_play_url=include_play_url,
    )


@router.post("/{collection_id}/items", status_code=status.HTTP_204_NO_CONTENT)
def add_collection_item(
    collection_id: int,
    payload: VideoCollectionItemAddRequest,
    current_user=Depends(get_current_user),
):
    VideoCollectionService.add_item(collection_id, payload.video_item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{collection_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_collection_item(
    collection_id: int, item_id: int, current_user=Depends(get_current_user)
):
    VideoCollectionService.remove_item(collection_id, item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{collection_id}/items/reorder", response_model=List[VideoCollectionItemResource])
def reorder_collection_items(
    collection_id: int,
    payload: VideoCollectionReorderRequest,
    current_user=Depends(get_current_user),
):
    return VideoCollectionService.reorder_items(collection_id, payload.ordered_item_ids)


@router.get("/{collection_id}/playlist.m3u8")
def get_video_collection_playlist(
    collection_id: int,
    expires: int | None = None,
    signature: str | None = None,
):
    # 与 stream 接口模式一致：m3u8 走签名 URL，不挂账号鉴权（前端 media_kit 不带 Cookie）。
    if expires is None or not signature:
        raise ApiError(403, "file_signature_invalid", "文件签名无效")

    verify_video_collection_playlist_signature(collection_id, expires, signature)
    content = VideoCollectionService.build_playlist(collection_id)
    return PlainTextResponse(
        content,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/{collection_id}/thumbnails",
    response_model=List[VideoCollectionThumbnailResource],
)
def list_video_collection_thumbnails(
    collection_id: int,
    current_user=Depends(get_current_user),
):
    return VideoCollectionService.list_collection_thumbnails(collection_id)
