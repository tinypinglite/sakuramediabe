import re
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


def parse_movie_number_from_path(file_path: str) -> str:
    split_parts = (file_path or "").split("/")
    if len(split_parts) > 2:
        target = "/".join(split_parts[-2:])
    else:
        target = split_parts[-1]
    return parse_movie_number_from_text(target)


def normalize_movie_number(value: str) -> str:
    normalized = (value or "").strip().upper()
    normalized = normalized.replace(" ", "")
    normalized = normalized.replace("_", "-")
    normalized = normalized.replace("PPV-", "")
    return normalized
