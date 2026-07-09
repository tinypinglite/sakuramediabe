import json
from datetime import datetime

from fastapi.encoders import jsonable_encoder

from src.common.runtime_time import serialize_runtime_local


def to_sse_event(event: str, payload: dict) -> str:
    # SSE 事件先转为 JSON 安全结构，避免 datetime 等对象中断流式响应。
    encoded_payload = jsonable_encoder(
        payload,
        custom_encoder={datetime: serialize_runtime_local},
    )
    return f"event: {event}\ndata: {json.dumps(encoded_payload, ensure_ascii=False)}\n\n"
