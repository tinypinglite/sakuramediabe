import pytest

from src.api.exception.errors import ApiError
from src.common.file_signatures import build_signed_media_url, verify_media_signature


def test_media_resource_path_accepts_relative_segments():
    url = build_signed_media_url(7, "hls/segment-0.ts")

    assert "/media/7/play/hls/segment-0.ts?" in url
    assert url.endswith("&delivery=proxy")


def test_media_url_accepts_redirect_delivery_without_changing_signature_scope():
    url = build_signed_media_url(7, delivery="redirect")

    assert url.endswith("&delivery=redirect")


@pytest.mark.parametrize(
    "resource_path",
    ("/segment.ts", "hls//segment.ts", "hls/./segment.ts", "hls/../segment.ts", "hls\\segment.ts", "hls\x00segment.ts"),
)
def test_media_resource_path_rejects_unsafe_syntax(resource_path):
    with pytest.raises(ApiError) as error:
        verify_media_signature(7, resource_path, 2_000_000_000, "invalid")

    assert error.value.code == "file_path_invalid"
