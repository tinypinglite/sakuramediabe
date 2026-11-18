from io import BytesIO

import pytest
from PIL import Image as PillowImage

from src.service.catalog.movie_image_service import (
    ImageDownloadError,
    MovieImageService,
)


class _Response:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def request(self, method: str, url: str) -> _Response:
        assert method == "GET"
        self.urls.append(url)
        return self.responses.pop(0)


def _png_bytes() -> bytes:
    output = BytesIO()
    PillowImage.new("RGB", (2, 2), (255, 0, 0)).save(output, format="PNG")
    return output.getvalue()


def _service(client: _Client, monkeypatch, retries: int = 1) -> MovieImageService:
    service = MovieImageService()
    service.http_client = client
    monkeypatch.setattr(MovieImageService, "IMAGE_DOWNLOAD_MAX_RETRIES", retries)
    monkeypatch.setattr(
        "src.service.catalog.movie_image_service.time.sleep", lambda _: None
    )
    return service


def test_download_image_rejects_invalid_bytes_without_writing(tmp_path, monkeypatch):
    client = _Client([_Response(b"not-an-image")])
    service = _service(client, monkeypatch)
    target = tmp_path / "image.jpg"

    with pytest.raises(ImageDownloadError, match="invalid_image"):
        service._download_image("https://example.com/image.jpg", target)

    assert not target.exists()


def test_download_image_retries_app_cdn_with_cache_buster(tmp_path, monkeypatch):
    valid = _png_bytes()
    client = _Client([_Response(b"not-an-image"), _Response(valid)])
    service = _service(client, monkeypatch, retries=2)
    target = tmp_path / "image.jpg"

    service._download_image("https://tp.spfcas.com/tenant/image.jpg", target)

    assert target.read_bytes() == valid
    assert client.urls[0] == "https://tp.spfcas.com/tenant/image.jpg"
    assert client.urls[1].startswith(
        "https://tp.spfcas.com/tenant/image.jpg?v="
    )


def test_download_image_does_not_cache_bust_other_hosts(tmp_path, monkeypatch):
    client = _Client([_Response(b"not-an-image"), _Response(_png_bytes())])
    service = _service(client, monkeypatch, retries=2)
    target = tmp_path / "image.jpg"

    service._download_image("https://example.com/image.jpg", target)

    assert client.urls[1] == "https://example.com/image.jpg"


def test_download_image_falls_back_to_github_raw_on_jsdelivr_403(
    tmp_path, monkeypatch
):
    valid = _png_bytes()
    client = _Client([_Response(b"", status_code=403), _Response(valid)])
    service = _service(client, monkeypatch)
    target = tmp_path / "20.jpg"
    image_url = (
        "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/"
        "Content/z-DMM(%E9%AA%91)/AI-Fix-%E7%AD%A7%E3%82%B8%E3%83%A5%E3%83%B3.jpg"
        "?t=1607433759"
    )

    service._download_image(image_url, target)

    assert client.urls == [
        image_url,
        (
            "https://raw.githubusercontent.com/xinxin8816/gfriends/HEAD/"
            "Content/z-DMM(%E9%AA%91)/AI-Fix-%E7%AD%A7%E3%82%B8%E3%83%A5%E3%83%B3.jpg"
            "?t=1607433759"
        ),
    ]
    assert target.read_bytes() == valid


def test_download_image_falls_back_once_and_stops_on_fallback_failure(
    tmp_path, monkeypatch
):
    client = _Client(
        [_Response(b"", status_code=403), _Response(b"", status_code=403)]
    )
    service = _service(client, monkeypatch, retries=3)
    target = tmp_path / "20.jpg"
    image_url = (
        "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/"
        "Content/z-DMM(%E9%AA%91)/AI-Fix-%E7%AD%A7%E3%82%B8%E3%83%A5%E3%83%B3.jpg"
        "?t=1607433759"
    )

    with pytest.raises(
        ImageDownloadError, match="fallback_unexpected_status_code:403"
    ):
        service._download_image(image_url, target)

    assert len(client.urls) == 2
    assert client.urls[1].startswith(
        "https://raw.githubusercontent.com/xinxin8816/gfriends/HEAD/"
    )
    assert not target.exists()


def test_download_image_does_not_fallback_on_non_gfriends_403(
    tmp_path, monkeypatch
):
    client = _Client(
        [_Response(b"", status_code=403), _Response(_png_bytes())]
    )
    service = _service(client, monkeypatch, retries=2)
    target = tmp_path / "image.jpg"
    image_url = "https://example.com/image.jpg"

    service._download_image(image_url, target)

    assert client.urls == [image_url, image_url]
    assert target.read_bytes()


def test_download_image_fallback_preserves_jsdelivr_version(
    tmp_path, monkeypatch
):
    valid = _png_bytes()
    client = _Client([_Response(b"", status_code=403), _Response(valid)])
    service = _service(client, monkeypatch)
    target = tmp_path / "20.jpg"
    image_url = (
        "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends@master/"
        "Content/z-DMM(%E9%AA%91)/AI-Fix-%E7%AD%A7%E3%82%B8%E3%83%A5%E3%83%B3.jpg"
    )

    service._download_image(image_url, target)

    assert client.urls[1] == (
        "https://raw.githubusercontent.com/xinxin8816/gfriends/master/"
        "Content/z-DMM(%E9%AA%91)/AI-Fix-%E7%AD%A7%E3%82%B8%E3%83%A5%E3%83%B3.jpg"
    )
    assert target.read_bytes() == valid


def test_download_image_keeps_valid_existing_file(tmp_path, monkeypatch):
    valid = _png_bytes()
    target = tmp_path / "image.jpg"
    target.write_bytes(valid)
    client = _Client([])
    service = _service(client, monkeypatch)

    service._download_image("https://example.com/image.jpg", target)

    assert client.urls == []
    assert target.read_bytes() == valid


def test_download_image_redownloads_invalid_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "image.jpg"
    target.write_bytes(b"not-an-image")
    valid = _png_bytes()
    client = _Client([_Response(valid)])
    service = _service(client, monkeypatch)

    service._download_image("https://example.com/image.jpg", target)

    assert client.urls == ["https://example.com/image.jpg"]
    assert target.read_bytes() == valid
