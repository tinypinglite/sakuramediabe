from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse

from src.api.exception.errors import ApiError
from src.api.routers._utils import parse_csv_positive_ints, require_signed_params
from src.api.routers.deps import db_deps, get_current_user
from src.common import build_signed_media_url, verify_media_signature
from src.model import Media
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    PlaybackContext,
    PlaybackDelivery,
    ProviderOperationError,
    ProviderUnavailableError,
)
from src.schema.common.pagination import PageResponse
from src.schema.playback.media import (
    InvalidMediaResource,
    MediaListItemResource,
    MediaPointCreateRequest,
    MediaPointKind,
    MediaPointResource,
    MediaProgressResource,
    MediaProgressUpdateRequest,
    MediaThumbnailGenerationState,
    MediaThumbnailResource,
)
from src.service.playback import MediaService
from src.service.playback.provider_helpers import library_handle_for, media_handle_for

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
    thumbnail_generation_state: MediaThumbnailGenerationState | None = Query(default=None),
    sort: str | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
    current_user=Depends(get_current_user),
):
    return MediaService.list_media(
        kind=kind,
        library_id=library_id,
        actor_ids=parse_csv_positive_ints(actor_ids, "actor_ids", error_code="invalid_media_filter"),
        thumbnail_generation_state=thumbnail_generation_state,
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


@router.get("/{media_id}/play/{resource_path:path}")
async def play_media(
    request: Request,
    media_id: int,
    resource_path: str,
    expires: int | None = None,
    signature: str | None = None,
    delivery: Literal["auto", "proxy", "redirect"] = "auto",
):
    require_signed_params(expires, signature)

    normalized_path = verify_media_signature(media_id, resource_path, expires, signature)
    media = Media.get_or_none(Media.id == media_id)
    if media is None:
        raise ApiError(404, "media_not_found", "媒体不存在")
    library = media.library
    if library is None:
        raise ApiError(404, "media_library_not_found", "媒体库不存在")
    library_handle = library_handle_for(library)
    media_handle = media_handle_for(media)
    try:
        bundle = MEDIA_PROVIDER_REGISTRY.require(library_handle.provider_key)
        if delivery == "auto":
            effective_delivery: PlaybackDelivery = (
                "redirect" if "redirect" in bundle.playback_deliveries else "proxy"
            )
        elif delivery not in bundle.playback_deliveries:
            raise ApiError(
                422,
                "provider_playback_delivery_unsupported",
                "媒体提供方不支持该播放方式",
            )
        else:
            effective_delivery = delivery
        storage = MEDIA_PROVIDER_REGISTRY.storage_for(library_handle)
    except ProviderUnavailableError as exc:
        raise ApiError(
            503,
            "provider_not_installed",
            "媒体提供方未安装",
        ) from exc
    except ProviderOperationError as exc:
        status_code = {
            "source_not_found": 404,
            "authentication_failed": 401,
            "unavailable": 503,
            "invalid_config": 422,
            "unsupported": 422,
        }[exc.code]
        raise ApiError(
            status_code,
            f"provider_{exc.code}",
            exc.safe_message,
        ) from exc

    def context_for(actual_delivery: PlaybackDelivery) -> PlaybackContext:
        return PlaybackContext(
            request=request,
            resource_path=normalized_path,
            delivery=actual_delivery,
            url_for=lambda path: build_signed_media_url(
                media.id, path, delivery=actual_delivery
            ),
        )

    try:
        return await storage.handle_playback(
            media=media_handle,
            context=context_for(effective_delivery),
        )
    except ProviderOperationError as exc:
        if (
            delivery == "auto"
            and effective_delivery == "redirect"
            and (exc.code == "unsupported" or exc.retryable)
        ):
            try:
                return await storage.handle_playback(
                    media=media_handle,
                    context=context_for("proxy"),
                )
            except ProviderOperationError as fallback_exc:
                exc = fallback_exc
        status_code = {
            "source_not_found": 404,
            "authentication_failed": 401,
            "unavailable": 503,
            "invalid_config": 422,
            "unsupported": 422,
        }[exc.code]
        raise ApiError(
            status_code,
            f"provider_{exc.code}",
            exc.safe_message,
        ) from exc


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
