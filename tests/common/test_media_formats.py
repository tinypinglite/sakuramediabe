import pytest

from src.common.media_formats import (
    SUPPORTED_VIDEO_EXTENSIONS,
    is_supported_video_file_name,
    normalize_media_resolution,
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("720X1280", "720x1280"),
        (" 001920x01080 ", "1920x1080"),
        (None, None),
        ("", None),
        ("1920", None),
        ("0x1080", None),
        ("1920x-1080", None),
        ("1920x1080x1", None),
        ("2147483648x1", None),
        ("1x2147483648", None),
        ("9" * 4_301 + "x1", None),
    ],
)
def test_normalize_media_resolution(value, expected) -> None:
    assert normalize_media_resolution(value) == expected
