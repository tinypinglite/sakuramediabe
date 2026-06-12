"""文件 Range 串流复用工具。

媒体原片与片段产物都按 HTTP Range(206) 串流，逻辑集中在此，避免在多个 router 重复实现。
"""

import os
from typing import BinaryIO

from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse


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


def range_requests_response(request: Request, file_path: str, content_type: str) -> StreamingResponse:
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
