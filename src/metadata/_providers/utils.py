from __future__ import annotations

import base64
from datetime import datetime, timezone

# 番号解析与规范化的公共实现已收敛到 src.common.movie_numbers，此处仅做转发以保持既有导入路径可用。
from src.common.movie_numbers import (
    MOVIE_NUMBER_PATTERNS,
    normalize_movie_number,
    parse_movie_number_from_text,
    remove_disturb,
)

__all__ = [
    "MOVIE_NUMBER_PATTERNS",
    "normalize_movie_number",
    "parse_movie_number_from_text",
    "remove_disturb",
    "parse_external_datetime",
    "b64url_encode",
    "b64url_decode",
]


def parse_external_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))
