from pathlib import Path
from typing import List

from src.config.config import settings

SUBTITLE_TAG = "中字"
BLURAY_TAG = "4K"
UNCENSORED_TAG = "无码"
VR_TAG = "VR"
NORMAL_TAG = "普通"


def detect_special_tags(
    text: str,
    movie_number: str,
    *,
    size_bytes: int | None = None,
    suffix: str | None = None,
) -> List[str]:
    lower_text = text.lower()
    movie_number_upper = movie_number.upper()
    ordered_tags: List[str] = []

    if _contains_any(lower_text, settings.media.inner_sub_tags):
        ordered_tags.append(SUBTITLE_TAG)
    if _is_4k(lower_text, size_bytes=size_bytes, suffix=suffix):
        ordered_tags.append(BLURAY_TAG)
    if _is_uncensored(lower_text, movie_number_upper):
        ordered_tags.append(UNCENSORED_TAG)
    if "vr" in lower_text or "VR" in movie_number_upper:
        ordered_tags.append(VR_TAG)

    return ordered_tags


def build_special_tags_text(file_path: Path, movie_number: str) -> str:
    tags = detect_special_tags(
        str(file_path),
        movie_number,
        size_bytes=file_path.stat().st_size,
        suffix=file_path.suffix.lower(),
    )
    if not tags:
        return NORMAL_TAG
    return " ".join(tags)


def detect_candidate_tags(title: str, movie_number: str, size_bytes: int) -> List[str]:
    return detect_special_tags(title, movie_number, size_bytes=size_bytes)


def _contains_any(lower_text: str, candidates: set[str]) -> bool:
    for candidate in candidates:
        if candidate.lower() in lower_text:
            return True
    return False


def _is_4k(lower_text: str, *, size_bytes: int | None = None, suffix: str | None = None) -> bool:
    for candidate in settings.media.blueray_tags:
        if candidate.lower() in lower_text:
            return True
    if suffix == ".iso":
        return True
    return bool(size_bytes is not None and size_bytes >= 18 * 1024 * 1024 * 1024)


def _is_uncensored(lower_text: str, movie_number_upper: str) -> bool:
    for prefix in settings.media.uncensored_prefix:
        if movie_number_upper.startswith(prefix.upper()):
            return True
    for candidate in settings.media.uncensored_tags:
        if candidate.lower() in lower_text:
            return True
    return False
