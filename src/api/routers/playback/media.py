import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.common import verify_media_signature
from src.common.range_streaming import range_requests_response
from src.model import Media
from src.schema.common.pagination import PageResponse
from src.schema.playback.media import (
    InvalidMediaResource,
    MediaPointCreateRequest,
    MediaPointResource,
    MediaProgressResource,
    MediaProgressUpdateRequest,
    MediaThumbnailResource,
    MediaValidityCheckResponse,
)
from src.service.playback import MediaService

router = APIRouter(
    prefix="/media",
    tags=["media"],
    dependencies=[Depends(db_deps)],
)


@router.get("/invalid", response_model=PageResponse[InvalidMediaResource])
def list_invalid_media(
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    current_user=Depends(get_current_user),
):
    return MediaService.list_invalid_media(page=page, page_size=page_size, search=search)


@router.post("/{media_id}/validity-check", response_model=MediaValidityCheckResponse)
def check_media_validity(
    media_id: int,
    current_user=Depends(get_current_user),
):
    return MediaService.check_media_validity(media_id)


@router.get("/{media_id}/points", response_model=list[MediaPointResource])
def list_media_points_for_media(
    media_id: int,
    current_user=Depends(get_current_user),
):
    return MediaService.list_points(media_id)


@router.post("/{media_id}/points", response_model=MediaPointResource)
def create_media_point(
    media_id: int,
    payload: MediaPointCreateRequest,
    current_user=Depends(get_current_user),
):
    resource, created = MediaService.create_point(media_id, payload)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=resource.model_dump(mode="json"),
    )


@router.delete("/{media_id}/points/{point_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_media_point(
    media_id: int,
    point_id: int,
    current_user=Depends(get_current_user),
):
    MediaService.delete_point(media_id, point_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{media_id}/stream")
async def stream_media_file(
    request: Request,
    media_id: int,
    expires: int | None = None,
    signature: str | None = None,
):
    if expires is None or not signature:
        raise ApiError(403, "file_signature_invalid", "文件签名无效")

    verify_media_signature(media_id, expires, signature)
    media = Media.get_or_none(Media.id == media_id)
    if media is None:
        raise ApiError(404, "media_not_found", "媒体不存在")

    # cloud115 库：现拿绑定请求方 UA 的直链后 302（签名 URL 保护 /stream，302 之后是 115 CDN）。
    # signature 参与直链缓存键：换一次签名 URL（前端换会话/超 12h 续签）就重取。
    if MediaService.is_cloud115_media(media):
        user_agent = request.headers.get("user-agent") or "SakuraMedia-Player/1.0"
        direct_url = await MediaService.resolve_cloud115_stream_url(
            media, user_agent, signature
        )
        return RedirectResponse(direct_url, status_code=status.HTTP_302_FOUND)

    if not media.path:
        raise ApiError(404, "file_not_found", "文件不存在")
    absolute_path = Path(media.path).expanduser().resolve()
    if not absolute_path.exists() or not absolute_path.is_file():
        raise ApiError(404, "file_not_found", "文件不存在")

    content_type, _ = mimetypes.guess_type(str(absolute_path))
    return range_requests_response(
        request,
        file_path=str(absolute_path),
        content_type=content_type or "application/octet-stream",
    )


@router.put("/{media_id}/progress", response_model=MediaProgressResource)
def update_media_progress(
    media_id: int,
    payload: MediaProgressUpdateRequest,
    current_user=Depends(get_current_user),
):
    return MediaService.update_progress(media_id, payload)


@router.get("/{media_id}/thumbnails", response_model=list[MediaThumbnailResource])
def list_media_thumbnails(
    media_id: int,
    current_user=Depends(get_current_user),
):
    return MediaService.list_thumbnails(media_id)


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_media(
    media_id: int,
    current_user=Depends(get_current_user),
):
    MediaService.delete_media(media_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
