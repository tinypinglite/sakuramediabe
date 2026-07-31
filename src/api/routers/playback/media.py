from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from src.api.exception.errors import ApiError
from src.api.routers._utils import (
    parse_csv_positive_ints,
    require_existing_file,
    require_signed_params,
    stream_local_file_response,
)
from src.api.routers.deps import db_deps, get_current_user
from src.common import verify_media_signature
from src.common.range_streaming import merged_range_requests_response
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
from src.service.playback import MediaService
from src.service.playback.merged_playback_service import MergedPlaybackService

router = APIRouter(
    prefix="/media",
    tags=["media"],
    dependencies=[Depends(db_deps)],
)


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
        actor_ids=parse_csv_positive_ints(actor_ids, "actor_ids", error_code="invalid_media_filter"),
        rapid_upload_status=rapid_upload_status,
        sort=sort,
        page=page,
        page_size=page_size,
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
    require_signed_params(expires, signature)

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
    require_existing_file(absolute_path)

    return stream_local_file_response(request, absolute_path, "application/octet-stream")

@router.get("/merged-stream")
def stream_merged_media_file(
    request: Request,
    media_ids: str | None = Query(default=None),
    expires: int | None = None,
    signature: str | None = None,
):
    """影片多分段合并播放流：把显式指定的本地分段虚拟合并成一个逻辑 mp4。

    签名复用 ``media_ids`` 中第一个分段的媒体签名（与 ``/{media_id}/stream`` 同机制）。
    后端校验：分段全部存在、全部本地库、文件都在、且全部归属同一部影片。
    """
    require_signed_params(expires, signature)
    ids = parse_csv_positive_ints(media_ids, "media_ids", error_code="invalid_media_filter")
    if not ids:
        raise ApiError(422, "merged_mp4_need_at_least_two", "合并播放至少需要 2 个分段")

    verify_media_signature(ids[0], expires, signature)
    layout = MergedPlaybackService.build_for_media_ids(ids)
    return merged_range_requests_response(request, layout, "video/mp4")

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
