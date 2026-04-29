import mimetypes
import os
from typing import BinaryIO

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.common import resolve_media_file_path, verify_media_signature
from src.schema.common.pagination import PageResponse
from src.schema.playback.media import (
    MediaListItemResource,
    MediaPointCreateRequest,
    MediaPointResource,
    MediaProgressResource,
    MediaProgressUpdateRequest,
    MediaThumbnailResource,
)
from src.service.playback import MediaService

router = APIRouter(
    prefix="/media",
    tags=["media"],
    dependencies=[Depends(db_deps)],
)


def _send_bytes_range_requests(file_obj: BinaryIO, start: int, end: int, chunk_size: int = 10_000):
    with file_obj as stream:
        stream.seek(start)
        while (position := stream.tell()) <= end:
            read_size = min(chunk_size, end + 1 - position)
            yield stream.read(read_size)


def _get_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    def _invalid_range() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=f"Invalid request range (Range:{range_header!r})",
        )

    try:
        start_text, end_text = range_header.replace("bytes=", "", 1).split("-", 1)
        start = int(start_text) if start_text else 0
        end = int(end_text) if end_text else file_size - 1
    except ValueError as exc:
        raise _invalid_range() from exc

    if start > end or start < 0 or end > file_size - 1:
        raise _invalid_range()
    return start, end


def _range_requests_response(request: Request, file_path: str, content_type: str) -> StreamingResponse:
    actual_file_size = os.stat(file_path).st_size
    range_header = request.headers.get("range")

    headers = {
        "content-type": content_type,
        "accept-ranges": "bytes",
        "content-encoding": "identity",
        "content-length": str(actual_file_size),
        "access-control-expose-headers": (
            "content-type, accept-ranges, content-length, "
            "content-range, content-encoding"
        ),
    }
    start = 0
    end = actual_file_size - 1
    status_code = status.HTTP_200_OK

    if range_header is not None:
        start, end = _get_range_header(range_header, actual_file_size)
        size = end - start + 1
        headers["content-length"] = str(size)
        headers["content-range"] = f"bytes {start}-{end}/{actual_file_size}"
        status_code = status.HTTP_206_PARTIAL_CONTENT

    return StreamingResponse(
        _send_bytes_range_requests(open(file_path, mode="rb"), start, end),
        headers=headers,
        status_code=status_code,
    )


@router.get("", response_model=PageResponse[MediaListItemResource])
def list_media(
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    sort: str | None = Query(default=None),
    valid: bool | None = Query(default=None),
    current_user=Depends(get_current_user),
):
    return MediaService.list_media(
        page=page,
        page_size=page_size,
        sort=sort,
        valid=valid,
    )


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
def stream_media_file(
    request: Request,
    media_id: int,
    expires: int | None = None,
    signature: str | None = None,
):
    if expires is None or not signature:
        raise ApiError(403, "file_signature_invalid", "文件签名无效")

    verify_media_signature(media_id, expires, signature)
    absolute_path = resolve_media_file_path(media_id)
    if not absolute_path.exists() or not absolute_path.is_file():
        raise ApiError(404, "file_not_found", "文件不存在")

    content_type, _ = mimetypes.guess_type(str(absolute_path))
    return _range_requests_response(
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
