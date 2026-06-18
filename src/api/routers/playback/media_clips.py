import mimetypes

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.common import resolve_media_clip_file_path, verify_clip_signature
from src.common.range_streaming import range_requests_response
from src.schema.common.pagination import PageResponse
from src.schema.playback.clips import (
    MediaClipCreateRequest,
    MediaClipDetailResource,
    MediaClipResource,
    MediaClipThumbnailResource,
    MediaClipUpdateRequest,
)
from src.service.playback import MediaClipService

router = APIRouter(
    tags=["media-clips"],
    dependencies=[Depends(db_deps)],
)


@router.post("/media/{media_id}/clips", response_model=MediaClipResource)
def create_media_clip(
    media_id: int,
    payload: MediaClipCreateRequest,
    current_user=Depends(get_current_user),
):
    resource, created = MediaClipService.create_clip(media_id, payload)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=resource.model_dump(mode="json"),
    )


@router.get("/media/{media_id}/clips", response_model=list[MediaClipResource])
def list_clips_for_media(
    media_id: int,
    current_user=Depends(get_current_user),
):
    return MediaClipService.list_clips(media_id)


@router.get("/media-clips", response_model=PageResponse[MediaClipResource])
def list_media_clips(
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    sort: str | None = Query(default=None),
    current_user=Depends(get_current_user),
):
    return MediaClipService.list_media_clips(page=page, page_size=page_size, sort=sort)


@router.get("/media-clips/{clip_id}", response_model=MediaClipDetailResource)
def get_media_clip(
    clip_id: int,
    current_user=Depends(get_current_user),
):
    return MediaClipService.get_clip_detail(clip_id)


@router.get("/media-clips/{clip_id}/thumbnails", response_model=list[MediaClipThumbnailResource])
def list_media_clip_thumbnails(
    clip_id: int,
    current_user=Depends(get_current_user),
):
    return MediaClipService.list_clip_thumbnails(clip_id)


@router.patch("/media-clips/{clip_id}", response_model=MediaClipResource)
def update_media_clip(
    clip_id: int,
    payload: MediaClipUpdateRequest,
    current_user=Depends(get_current_user),
):
    return MediaClipService.update_clip(clip_id, payload)


@router.delete("/media-clips/{clip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_media_clip(
    clip_id: int,
    current_user=Depends(get_current_user),
):
    MediaClipService.delete_clip(clip_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/media-clips/{clip_id}/stream")
def stream_media_clip(
    request: Request,
    clip_id: int,
    expires: int | None = None,
    signature: str | None = None,
):
    if expires is None or not signature:
        raise ApiError(403, "file_signature_invalid", "文件签名无效")

    verify_clip_signature(clip_id, expires, signature)
    absolute_path = resolve_media_clip_file_path(clip_id)
    if not absolute_path.exists() or not absolute_path.is_file():
        raise ApiError(404, "file_not_found", "文件不存在")

    content_type, _ = mimetypes.guess_type(str(absolute_path))
    return range_requests_response(
        request,
        file_path=str(absolute_path),
        content_type=content_type or "video/mp4",
    )
