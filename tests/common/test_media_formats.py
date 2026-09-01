from src.common.media_formats import (
    SUPPORTED_VIDEO_EXTENSIONS,
    is_supported_video_file_name,
)


def test_supported_video_extensions_match_host_policy() -> None:
    assert SUPPORTED_VIDEO_EXTENSIONS == {
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


def test_iso_is_not_a_supported_video_extension() -> None:
    assert is_supported_video_file_name("SMBD-110.iso") is False
