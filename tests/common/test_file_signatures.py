import pytest

from src.api.exception.errors import ApiError
from src.common.file_signatures import (
    build_signed_media_url,
    build_signed_merged_media_url,
    verify_media_signature,
)


def test_media_resource_path_accepts_relative_segments():
    url = build_signed_media_url(7, "hls/segment-0.ts")

    assert "/media/7/play/hls/segment-0.ts?" in url
    assert url.endswith("&delivery=proxy")


def test_media_url_accepts_redirect_delivery_without_changing_signature_scope():
    url = build_signed_media_url(7, delivery="redirect")

    assert url.endswith("&delivery=redirect")


def test_merged_media_url_binds_ordered_media_ids_and_resource_path():
    url = build_signed_merged_media_url((7, 8), "index.m3u8")

    assert "/media/merged-play/index.m3u8?media_ids=7,8" in url


@pytest.mark.parametrize("media_ids", ((7,), (7, 7), (0, 7)))
def test_merged_media_url_rejects_invalid_media_ids(media_ids):
    with pytest.raises(ValueError):
        build_signed_merged_media_url(media_ids, "index.m3u8")


@pytest.mark.parametrize(
    "resource_path",
    ("/segment.ts", "hls//segment.ts", "hls/./segment.ts", "hls/../segment.ts", "hls\\segment.ts", "hls\x00segment.ts"),
)
def test_media_resource_path_rejects_unsafe_syntax(resource_path):
    with pytest.raises(ApiError) as error:
        verify_media_signature(7, resource_path, 2_000_000_000, "invalid")

    assert error.value.code == "file_path_invalid"


def test_media_url_rejects_auto_delivery():
    with pytest.raises(ValueError, match="unsupported playback delivery"):
        build_signed_media_url(7, delivery="auto")
