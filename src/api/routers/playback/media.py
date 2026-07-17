import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.common import verify_media_signature
from src.common.range_streaming import range_requests_response
from src.model import Media
from src.schema.common.pagination import PageResponse
from src.schema.playback.media import (
    InvalidMediaResource,
    MediaListItemResource,
    MediaPointCreateRequest,
    MediaPointKind,
    MediaPointResource,
    MediaProgressResource,
    MediaProgressUpdateRequest,
    MediaRapidUploadFilterStatus,
    MediaThumbnailResource,
    MediaValidityCheckResponse,
)
from src.schema.transfers.rapid_upload import (
    MediaRapidUploadBatchListItemResource,
    MediaRapidUploadBatchResource,
    MediaRapidUploadCreateRequest,
    MediaRapidUploadTriggerResponse,
)
from src.service.playback import MediaService
from src.service.transfers import MediaRapidUploadService

router = APIRouter(
    prefix="/media",
    tags=["media"],
    dependencies=[Depends(db_deps)],
)


def _parse_csv_positive_ints(raw: str | None, field_name: str) -> list[int] | None:
    if raw is None:
        return None

    # 数组筛选参数必须显式传入正整数，避免把空串或脏值静默吞掉。
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part for part in parts):
        raise ApiError(
            422,
            "invalid_media_filter",
            "Invalid filter value",
            {field_name: raw},
        )

    try:
        values = [int(part) for part in parts]
    except ValueError as exc:
        raise ApiError(
            422,
            "invalid_media_filter",
            "Invalid filter value",
            {field_name: raw},
        ) from exc

    if any(value <= 0 for value in values):
        raise ApiError(
            422,
            "invalid_media_filter",
            "Invalid filter value",
            {field_name: raw},
        )
    return values


@router.get("", response_model=PageResponse[MediaListItemResource])
def list_media(
    kind: MediaPointKind = Query(default=MediaPointKind.ALL),
    library_id: int | None = Query(default=None),
    actor_ids: str | None = Query(default=None),
    rapid_upload_status: MediaRapidUploadFilterStatus | None = Query(default=None),
    sort: str | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
    current_user=Depends(get_current_user),
):
    return MediaService.list_media(
        kind=kind,
        library_id=library_id,
        actor_ids=_parse_csv_positive_ints(actor_ids, "actor_ids"),
        rapid_upload_status=rapid_upload_status,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/rapid-uploads",
    response_model=MediaRapidUploadTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_media_rapid_upload(
    payload: MediaRapidUploadCreateRequest,
    current_user=Depends(get_current_user),
):
    return MediaRapidUploadService.trigger_batch(
        media_ids=payload.media_ids,
        target_library_id=payload.target_library_id,
    )


@router.get(
    "/rapid-uploads",
    response_model=PageResponse[MediaRapidUploadBatchListItemResource],
)
def list_media_rapid_uploads(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user=Depends(get_current_user),
):
    return MediaRapidUploadService.list_batches(page=page, page_size=page_size)


@router.get(
    "/rapid-uploads/{batch_id}",
    response_model=MediaRapidUploadBatchResource,
)
def get_media_rapid_upload(
    batch_id: int,
    current_user=Depends(get_current_user),
):
    return MediaRapidUploadService.get_batch(batch_id)


@router.post(
    "/rapid-uploads/{batch_id}/retry",
    response_model=MediaRapidUploadTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_media_rapid_upload(
    batch_id: int,
    current_user=Depends(get_current_user),
):
    return MediaRapidUploadService.retry_batch(batch_id)


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

    # cloud115 库优先派发最高码率 HLS；HLS 暂不可用时服务层自动回落原画直链。
    if MediaService.is_cloud115_media(media):
        # 用 getlist 排查"客户端塞了多条 User-Agent"这类隐蔽情况——libmpv/ffmpeg 某些版本
        # 用 -headers 覆盖 UA 时会与默认 UA 同时出现在报文里，Starlette 只取第一条，与
        # CDN 侧解析的 UA 可能是不同一条，进而造成签名不一致。
        ua_list = request.headers.getlist("user-agent")
        user_agent = (ua_list[0] if ua_list else "") or "SakuraMedia-Player/1.0"
        from loguru import logger
        logger.info(
            "cloud115 stream request media_id={} ua_count={} ua_first={!r} ua_all={!r} sig={} client={}",
            media_id, len(ua_list), user_agent, ua_list, signature[:12], request.client.host if request.client else "?",
        )
        playback_url = await MediaService.resolve_cloud115_playback_url(
            media, user_agent, signature
        )
        return RedirectResponse(
            playback_url,
            status_code=status.HTTP_302_FOUND,
            # HLS 与直链均为短期签名地址，播放器重新打开时必须重新经过智能派发。
            headers={"Cache-Control": "no-store"},
        )

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
