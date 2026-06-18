from typing import List

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import PlainTextResponse

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.common import verify_clip_collection_playlist_signature
from src.schema.collections.clips import (
    ClipCollectionClipItemResource,
    ClipCollectionCreateRequest,
    ClipCollectionResource,
    ClipCollectionSetClipsRequest,
    ClipCollectionThumbnailResource,
    ClipCollectionUpdateRequest,
)
from src.schema.common.pagination import PageResponse
from src.service.collections import ClipCollectionService

router = APIRouter(
    prefix="/clip-collections",
    tags=["clip-collections"],
    dependencies=[Depends(db_deps)],
)


@router.get("", response_model=List[ClipCollectionResource])
def list_clip_collections(current_user=Depends(get_current_user)):
    return ClipCollectionService.list_collections()


@router.post("", response_model=ClipCollectionResource, status_code=status.HTTP_201_CREATED)
def create_clip_collection(
    payload: ClipCollectionCreateRequest,
    current_user=Depends(get_current_user),
):
    return ClipCollectionService.create_collection(payload)


@router.get("/{collection_id}", response_model=ClipCollectionResource)
def get_clip_collection(collection_id: int, current_user=Depends(get_current_user)):
    return ClipCollectionService.get_collection(collection_id)


@router.patch("/{collection_id}", response_model=ClipCollectionResource)
def update_clip_collection(
    collection_id: int,
    payload: ClipCollectionUpdateRequest,
    current_user=Depends(get_current_user),
):
    return ClipCollectionService.update_collection(collection_id, payload)


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_clip_collection(collection_id: int, current_user=Depends(get_current_user)):
    ClipCollectionService.delete_collection(collection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{collection_id}/clips", response_model=PageResponse[ClipCollectionClipItemResource])
def list_clip_collection_clips(
    collection_id: int,
    page: int = 1,
    page_size: int = 20,
    current_user=Depends(get_current_user),
):
    return ClipCollectionService.list_collection_clips(
        collection_id=collection_id, page=page, page_size=page_size
    )


@router.put("/{collection_id}/clips/{clip_id}", status_code=status.HTTP_204_NO_CONTENT)
def add_clip_to_collection(
    collection_id: int,
    clip_id: int,
    current_user=Depends(get_current_user),
):
    ClipCollectionService.add_clip(collection_id, clip_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{collection_id}/clips/{clip_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_clip_from_collection(
    collection_id: int,
    clip_id: int,
    current_user=Depends(get_current_user),
):
    ClipCollectionService.remove_clip(collection_id, clip_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{collection_id}/clips", status_code=status.HTTP_204_NO_CONTENT)
def set_clip_collection_clips(
    collection_id: int,
    payload: ClipCollectionSetClipsRequest,
    current_user=Depends(get_current_user),
):
    ClipCollectionService.set_clips(collection_id, payload.clip_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{collection_id}/playlist.m3u8")
def get_clip_collection_playlist(
    collection_id: int,
    expires: int | None = None,
    signature: str | None = None,
):
    # 与 stream 接口模式一致：m3u8 走签名 URL，不挂全局账号鉴权（前端 media_kit 不带 Cookie）。
    if expires is None or not signature:
        raise ApiError(403, "file_signature_invalid", "文件签名无效")

    verify_clip_collection_playlist_signature(collection_id, expires, signature)
    content = ClipCollectionService.build_playlist(collection_id)
    # HLS 规范 MIME；显式不缓存避免合集成员变更后客户端继续读旧清单。
    return PlainTextResponse(
        content,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/{collection_id}/thumbnails",
    response_model=List[ClipCollectionThumbnailResource],
)
def list_clip_collection_thumbnails(
    collection_id: int,
    current_user=Depends(get_current_user),
):
    return ClipCollectionService.list_collection_thumbnails(collection_id)
