import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse

from src.api.exception.errors import ApiError
from src.api.routers._utils import parse_csv_positive_ints, require_signed_params
from src.api.routers.deps import db_deps, get_current_user
from src.common import (
    build_signed_media_url,
    build_signed_merged_media_url,
    verify_media_signature,
    verify_merged_media_signature,
)
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
    DuplicateMediaGroupResource,
    InvalidMediaResource,
    MediaListItemResource,
    MediaPlaybackModeResource,
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


_AUTO_REDIRECT_MAX_ATTEMPTS = 3
_AUTO_REDIRECT_RETRY_GAP_SECONDS = 1.5
_AUTO_REDIRECT_STATE_MAX_ENTRIES = 1024
_PLAYBACK_MODE_RESULT_TTL_SECONDS = 120.0
_PLAYBACK_MODE_RESULT_MAX_ENTRIES = 1024


@dataclass
class _AutoRedirectAttempt:
    last_seen_at: float
    count: int


class _AutoRedirectRetries:
    """Detect rapid client reopens of an auto redirect without persisting state."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = _AUTO_REDIRECT_STATE_MAX_ENTRIES,
    ):
        self._clock = clock
        self._max_entries = max_entries
        self._attempts: OrderedDict[tuple[int, str, str], _AutoRedirectAttempt] = (
            OrderedDict()
        )

    def should_use_proxy(self, *, media_id: int, request: Request) -> bool:
        client_host = request.client.host if request.client is not None else ""
        user_agent = request.headers.get("user-agent", "")
        user_agent_hash = hashlib.sha256(user_agent.encode()).hexdigest()
        key = (media_id, client_host, user_agent_hash)
        now = self._clock()
        previous = self._attempts.get(key)
        count = (
            previous.count + 1
            if previous is not None
            and now - previous.last_seen_at <= _AUTO_REDIRECT_RETRY_GAP_SECONDS
            else 1
        )
        self._attempts[key] = _AutoRedirectAttempt(last_seen_at=now, count=count)
        self._attempts.move_to_end(key)
        while len(self._attempts) > self._max_entries:
            self._attempts.popitem(last=False)
        return count > _AUTO_REDIRECT_MAX_ATTEMPTS


_AUTO_REDIRECT_RETRIES = _AutoRedirectRetries()


@dataclass
class _PlaybackModeResult:
    mode: Literal["direct", "proxy"]
    recorded_at: float


class _PlaybackModeResults:
    """Bounded, short-lived results for one player's actual gateway request."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = _PLAYBACK_MODE_RESULT_TTL_SECONDS,
        max_entries: int = _PLAYBACK_MODE_RESULT_MAX_ENTRIES,
    ) -> None:
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._results: OrderedDict[str, _PlaybackModeResult] = OrderedDict()

    def record(self, *, attempt_id: str, delivery: PlaybackDelivery) -> None:
        now = self._clock()
        self._discard_expired(now)
        self._results[attempt_id] = _PlaybackModeResult(
            mode="direct" if delivery == "redirect" else "proxy",
            recorded_at=now,
        )
        self._results.move_to_end(attempt_id)
        while len(self._results) > self._max_entries:
            self._results.popitem(last=False)

    def get(self, attempt_id: str) -> Literal["direct", "proxy"] | None:
        now = self._clock()
        self._discard_expired(now)
        result = self._results.get(attempt_id)
        if result is None:
            return None
        self._results.move_to_end(attempt_id)
        return result.mode

    def _discard_expired(self, now: float) -> None:
        expired_ids = [
            attempt_id
            for attempt_id, result in self._results.items()
            if now - result.recorded_at > self._ttl_seconds
        ]
        for attempt_id in expired_ids:
            self._results.pop(attempt_id, None)


_PLAYBACK_MODE_RESULTS = _PlaybackModeResults()


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


@router.get("/duplicates", response_model=PageResponse[DuplicateMediaGroupResource])
def list_duplicate_media_groups(
    kind: Literal["jav", "video"] = Query(...),
    page: int = 1,
    page_size: int = 20,
    current_user=Depends(get_current_user),
):
    return MediaService.list_duplicate_media_groups(
        kind=kind,
        page=page,
        page_size=page_size,
    )


def _parse_merged_media_ids(raw: str | None) -> tuple[int, ...]:
    media_ids = parse_csv_positive_ints(
        raw, "media_ids", error_code="invalid_merged_playback"
    )
    if media_ids is None or len(media_ids) < 2:
        raise ApiError(
            422,
            "merged_playback_need_at_least_two",
            "合并播放至少需要 2 个分段",
        )
    if len(set(media_ids)) != len(media_ids):
        raise ApiError(422, "invalid_merged_playback", "合并播放分段不可重复")
    return tuple(media_ids)


def _raise_provider_operation_error(exc: ProviderOperationError) -> None:
    status_code = {
        "source_not_found": 404,
        "authentication_failed": 401,
        "unavailable": 503,
        "invalid_config": 422,
        "unsupported": 422,
    }[exc.code]
    raise ApiError(status_code, f"provider_{exc.code}", exc.safe_message) from exc


@router.get("/playback-attempts/{attempt_id}", response_model=MediaPlaybackModeResource)
async def get_playback_attempt_mode(
    attempt_id: str,
    current_user=Depends(get_current_user),
):
    return MediaPlaybackModeResource(mode=_PLAYBACK_MODE_RESULTS.get(attempt_id))


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
    playback_attempt_id: str | None = Query(
        default=None,
        min_length=20,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
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
            if (
                not normalized_path
                and effective_delivery == "redirect"
                and _AUTO_REDIRECT_RETRIES.should_use_proxy(
                    media_id=media.id, request=request
                )
            ):
                effective_delivery = "proxy"
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
        _raise_provider_operation_error(exc)

    def context_for(actual_delivery: PlaybackDelivery) -> PlaybackContext:
        return PlaybackContext(
            request=request,
            resource_path=normalized_path,
            delivery=actual_delivery,
            url_for=lambda path: build_signed_media_url(
                media.id, path, delivery=actual_delivery
            ),
        )

    async def handle(actual_delivery: PlaybackDelivery):
        response = await storage.handle_playback(
            media=media_handle,
            context=context_for(actual_delivery),
        )
        if playback_attempt_id is not None and not normalized_path:
            _PLAYBACK_MODE_RESULTS.record(
                attempt_id=playback_attempt_id,
                delivery=actual_delivery,
            )
        return response

    try:
        return await handle(effective_delivery)
    except ProviderOperationError as exc:
        if (
            delivery == "auto"
            and effective_delivery == "redirect"
            and (exc.code == "unsupported" or exc.retryable)
        ):
            try:
                return await handle("proxy")
            except ProviderOperationError as fallback_exc:
                exc = fallback_exc
        _raise_provider_operation_error(exc)


@router.get("/merged-play/{resource_path:path}")
async def play_merged_media(
    request: Request,
    resource_path: str,
    media_ids: str | None = Query(default=None),
    expires: int | None = None,
    signature: str | None = None,
):
    require_signed_params(expires, signature)
    ordered_ids = _parse_merged_media_ids(media_ids)
    normalized_path = verify_merged_media_signature(
        ordered_ids, resource_path, expires, signature
    )
    media_by_id = {
        media.id: media
        for media in Media.select(Media).where(Media.id.in_(ordered_ids))
    }
    if len(media_by_id) != len(ordered_ids):
        raise ApiError(404, "media_not_found", "部分媒体不存在")
    medias = tuple(media_by_id[media_id] for media_id in ordered_ids)
    if any(not media.valid for media in medias):
        raise ApiError(422, "merged_playback_unavailable", "合并分段存在无效媒体")
    movie_numbers = {media.movie_number for media in medias}
    if len(movie_numbers) != 1 or None in movie_numbers:
        raise ApiError(422, "merged_playback_cross_movie", "合并分段必须属于同一部影片")
    library_ids = {media.library_id for media in medias}
    if len(library_ids) != 1 or None in library_ids:
        raise ApiError(422, "merged_playback_cross_library", "合并分段必须来自同一媒体库")
    library = medias[0].library
    if library is None:
        raise ApiError(404, "media_library_not_found", "媒体库不存在")
    library_handle = library_handle_for(library)
    try:
        bundle = MEDIA_PROVIDER_REGISTRY.require(library_handle.provider_key)
        if getattr(bundle, "merged_playback_format", None) not in {"mp4", "hls"}:
            raise ApiError(422, "merged_playback_unavailable", "媒体提供方不支持合并播放")
        storage = MEDIA_PROVIDER_REGISTRY.storage_for(library_handle)
    except ProviderUnavailableError as exc:
        raise ApiError(503, "provider_not_installed", "媒体提供方未安装") from exc
    except ProviderOperationError as exc:
        _raise_provider_operation_error(exc)

    handle_merged_playback = getattr(storage, "handle_merged_playback", None)
    if not callable(handle_merged_playback):
        raise ApiError(422, "merged_playback_unavailable", "媒体提供方不支持合并播放")
    context = PlaybackContext(
        request=request,
        resource_path=normalized_path,
        delivery="proxy",
        url_for=lambda path: build_signed_merged_media_url(ordered_ids, path),
    )
    try:
        return await handle_merged_playback(
            medias=tuple(media_handle_for(media) for media in medias),
            context=context,
        )
    except ProviderOperationError as exc:
        _raise_provider_operation_error(exc)


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
