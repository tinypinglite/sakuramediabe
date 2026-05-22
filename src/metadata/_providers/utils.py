from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import List, Pattern, Tuple

MOVIE_NUMBER_PATTERNS: List[Tuple[Pattern[str], int]] = [
    (re.compile(r"(DSVR)0(\d{3,4})", re.IGNORECASE), 2),
    (re.compile(r"(XXX)-(AV)-(\d+)", re.IGNORECASE), 3),
    (re.compile(r"(N\d{4})", re.IGNORECASE), 1),
    (re.compile(r"(LAFB?D?)-(\d+)", re.IGNORECASE), 2),
    (re.compile(r"(MISM)-(\d+)", re.IGNORECASE), 2),
    (re.compile(r"(MKB?D?)-(S\d+)", re.IGNORECASE), 2),
    (re.compile(r"(S2MB?D?)-(\d+)", re.IGNORECASE), 2),
    (re.compile(r"(CWPB?D?)-(\d+)", re.IGNORECASE), 2),
    (re.compile(r"(SMB?D?)-(\d+)", re.IGNORECASE), 2),
    (re.compile(r"(MCDV)-(\d+)", re.IGNORECASE), 2),
    (re.compile(r"(\d{6})_(\d{3})"), 2),
    (re.compile(r"(\d{6})-(\d{3})"), 2),
    (re.compile(r"(FC2)PPV_(\d+)", re.IGNORECASE), 2),
    (re.compile(r"(FC2)PPV-(\d+)", re.IGNORECASE), 2),
    (re.compile(r"(FC2)-PPV-(\d+)", re.IGNORECASE), 2),
    (re.compile(r"(FC2)-(\d+)", re.IGNORECASE), 2),
    (re.compile(r"9([a-zA-Z]{3,5})(\d{2,3})"), 2),
    (re.compile(r"(?<!\.)([a-zA-Z]{2,6})00(\d{3})"), 2),
    (re.compile(r"(?<!\.)([a-zA-Z]{2,6})-(\d{3,5})"), 2),
    (re.compile(r"(?<!\.)([a-zA-Z]{2,6})(\d{3,5})"), 2),
    (re.compile(r"([a-zA-Z]{3,5}) (\d{2,6})"), 2),
]


def remove_disturb(value: str) -> str:
    pattern = r"\b(?:[a-zA-Z0-9]+\.)*[a-zA-Z0-9]+\.(?:com|cn|net|org|gov|edu)\b"
    return re.sub(pattern, "", value)


def parse_movie_number_from_text(value: str) -> str:
    text = remove_disturb(value or "")
    for pattern, expected_group_count in MOVIE_NUMBER_PATTERNS:
        result = pattern.search(text)
        if result and len(result.groups()) == expected_group_count:
            return "-".join([str(part).upper() for part in result.groups()])
    return ""


def normalize_movie_number(value: str) -> str:
    normalized = (value or "").strip().upper()
    normalized = normalized.replace(" ", "")
    normalized = normalized.replace("_", "-")
    normalized = normalized.replace("PPV-", "")
    return normalized


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
