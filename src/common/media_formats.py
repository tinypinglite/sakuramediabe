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


def is_supported_video_file_name(file_name: str) -> bool:
    return Path(file_name).suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
