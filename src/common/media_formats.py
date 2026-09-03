from pathlib import Path

SUPPORTED_VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".f4v",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".rm",
        ".rmvb",
        ".ts",
        ".webm",
        ".wmv",
    }
)
_MAX_MEDIA_RESOLUTION_LENGTH = 32
_MAX_MEDIA_RESOLUTION_DIMENSION = "2147483647"


def is_supported_video_file_name(file_name: str) -> bool:
    return Path(file_name).suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def normalize_media_resolution(value: object) -> str | None:
    """Return provider dimensions as a canonical positive ``WxH`` string."""
    if not isinstance(value, str) or len(value) > _MAX_MEDIA_RESOLUTION_LENGTH:
        return None
    parts = value.strip().lower().split("x")
    if len(parts) != 2 or not all(part.isascii() and part.isdigit() for part in parts):
        return None
    dimensions = tuple(part.lstrip("0") for part in parts)
    if any(not dimension for dimension in dimensions):
        return None
    if any(
        len(dimension) > len(_MAX_MEDIA_RESOLUTION_DIMENSION)
        or (
            len(dimension) == len(_MAX_MEDIA_RESOLUTION_DIMENSION)
            and dimension > _MAX_MEDIA_RESOLUTION_DIMENSION
        )
        for dimension in dimensions
    ):
        return None
    return "x".join(dimensions)
