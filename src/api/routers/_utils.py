"""Router 层公用工具：查询参数解析、SSE 响应、签名文件流式响应。

原本分散在各域 router 里各写一份的小工具集中到这里，避免重复实现。
"""

import json
import mimetypes
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from src.api.exception.errors import ApiError
from src.common.range_streaming import range_requests_response
from src.common.runtime_time import serialize_runtime_local


def parse_csv_positive_ints(
    raw: str | None, field_name: str, *, error_code: str
) -> list[int] | None:
    if raw is None:
        return None

    # 数组筛选参数必须显式传入正整数，避免把空串或脏值静默吞掉。
    parts = [part.strip() for part in raw.split(",")]
    if not parts or any(not part for part in parts):
        raise ApiError(422, error_code, "Invalid filter value", {field_name: raw})

    try:
        values = [int(part) for part in parts]
    except ValueError as exc:
        raise ApiError(422, error_code, "Invalid filter value", {field_name: raw}) from exc

    if any(value <= 0 for value in values):
        raise ApiError(422, error_code, "Invalid filter value", {field_name: raw})
    return values


def parse_optional_exact_text(
    raw: str | None, field_name: str, *, error_code: str
) -> str | None:
    if raw is None:
        return None

    # 精确筛选参数不接受空白值，避免客户端误以为空串会被忽略。
    normalized = raw.strip()
    if not normalized:
        raise ApiError(422, error_code, "Invalid filter value", {field_name: raw})
    return normalized


def to_sse_event(event: str, payload: dict) -> str:
    # SSE 事件先转为 JSON 安全结构，避免 datetime 等对象中断流式响应。
    encoded_payload = jsonable_encoder(
        payload,
        custom_encoder={datetime: serialize_runtime_local},
    )
    return f"event: {event}\ndata: {json.dumps(encoded_payload, ensure_ascii=False)}\n\n"


def sse_streaming_response(event_stream: Iterable[str]) -> StreamingResponse:
    return StreamingResponse(
        event_stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def require_signed_params(expires: int | None, signature: str | None) -> None:
    if expires is None or not signature:
        raise ApiError(403, "file_signature_invalid", "文件签名无效")


def require_existing_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        raise ApiError(404, "file_not_found", "文件不存在")


def stream_local_file_response(
    request: Request, absolute_path: Path, default_content_type: str
) -> StreamingResponse:
    content_type, _ = mimetypes.guess_type(str(absolute_path))
    return range_requests_response(
        request,
        file_path=str(absolute_path),
        content_type=content_type or default_content_type,
    )
