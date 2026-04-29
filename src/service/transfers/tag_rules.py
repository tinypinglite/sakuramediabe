from typing import Any, Iterable, List

from src.config.config import settings

SUBTITLE_TAG = "中字"
BLURAY_TAG = "4K"
UNCENSORED_TAG = "无码"
VR_TAG = "VR"
NORMAL_TAG = "普通"
ORDERED_SPECIAL_TAGS = [SUBTITLE_TAG, BLURAY_TAG, UNCENSORED_TAG, VR_TAG]


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
    if _is_candidate_4k(lower_text, size_bytes=size_bytes, suffix=suffix):
        ordered_tags.append(BLURAY_TAG)
    if _is_uncensored(lower_text, movie_number_upper):
        ordered_tags.append(UNCENSORED_TAG)
    if "vr" in lower_text or "VR" in movie_number_upper:
        ordered_tags.append(VR_TAG)

    return ordered_tags


def detect_media_special_tags(
    texts: Iterable[str],
    movie_number: str,
    *,
    video_info: dict[str, Any] | None,
    has_subtitle: bool = False,
) -> List[str]:
    merged_text = " ".join(texts)
    lower_text = merged_text.lower()
    movie_number_upper = movie_number.upper()
    ordered_tags: List[str] = []

    if has_subtitle or _contains_any(lower_text, settings.media.inner_sub_tags):
        ordered_tags.append(SUBTITLE_TAG)
    if _is_media_4k(video_info):
        ordered_tags.append(BLURAY_TAG)
    if _is_uncensored(lower_text, movie_number_upper):
        ordered_tags.append(UNCENSORED_TAG)
    if "vr" in lower_text or "VR" in movie_number_upper:
        ordered_tags.append(VR_TAG)

    return ordered_tags


def build_media_special_tags(
    texts: Iterable[str],
    movie_number: str,
    *,
    video_info: dict[str, Any] | None,
    has_subtitle: bool = False,
) -> str:
    tags = detect_media_special_tags(
        texts,
        movie_number,
        video_info=video_info,
        has_subtitle=has_subtitle,
    )
    if not tags:
        return NORMAL_TAG
    return " ".join(tags)


def build_scanned_media_special_tags(
    existing_special_tags: str | None,
    *,
    video_info: dict[str, Any] | None,
    has_subtitle: bool = False,
) -> str:
    existing_tags = set(parse_special_tags_text(existing_special_tags))
    existing_tags.discard(BLURAY_TAG)
    if has_subtitle:
        existing_tags.add(SUBTITLE_TAG)
    if _is_media_4k(video_info):
        existing_tags.add(BLURAY_TAG)
    ordered_tags = [tag for tag in ORDERED_SPECIAL_TAGS if tag in existing_tags]
    if not ordered_tags:
        return NORMAL_TAG
    return " ".join(ordered_tags)


def parse_special_tags_text(value: str | None) -> List[str]:
    if value is None:
        return []
    parts = [part.strip() for part in value.split() if part.strip()]
    return [tag for tag in ORDERED_SPECIAL_TAGS if tag in parts]


def detect_candidate_tags(title: str, movie_number: str, size_bytes: int) -> List[str]:
    return detect_special_tags(title, movie_number, size_bytes=size_bytes)


def _contains_any(lower_text: str, candidates: set[str]) -> bool:
    for candidate in candidates:
        if candidate.lower() in lower_text:
            return True
    return False


def _is_candidate_4k(lower_text: str, *, size_bytes: int | None = None, suffix: str | None = None) -> bool:
    for candidate in settings.media.blueray_tags:
        if candidate.lower() in lower_text:
            return True
    if suffix == ".iso":
        return True
    return bool(size_bytes is not None and size_bytes >= 18 * 1024 * 1024 * 1024)


def _coerce_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _is_media_4k(video_info: dict[str, Any] | None) -> bool:
    if not isinstance(video_info, dict):
        return False
    video_payload = video_info.get("video")
    if not isinstance(video_payload, dict):
        return False
    width = _coerce_positive_int(video_payload.get("width"))
    height = _coerce_positive_int(video_payload.get("height"))
    if width is None or height is None:
        return False
    return width >= 3840 or height >= 2160


def _is_uncensored(lower_text: str, movie_number_upper: str) -> bool:
    for prefix in settings.media.uncensored_prefix:
        if movie_number_upper.startswith(prefix.upper()):
            return True
    for candidate in settings.media.uncensored_tags:
        if candidate.lower() in lower_text:
            return True
    return False
