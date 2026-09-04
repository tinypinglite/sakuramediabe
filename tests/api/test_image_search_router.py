from io import BytesIO

import pytest
from fastapi import UploadFile
from PIL import Image as PillowImage

from src.api.routers.discovery import image_search
from src.service.discovery.image_search_input import normalize_image_search_query


def _png_bytes() -> bytes:
    output = BytesIO()
    PillowImage.new("RGBA", (4, 3), (12, 34, 56, 128)).save(output, format="PNG")
    return output.getvalue()


def _animated_gif_bytes() -> bytes:
    first_frame = PillowImage.new("RGB", (4, 3), (255, 0, 0))
    second_frame = PillowImage.new("RGB", (4, 3), (0, 0, 255))
    output = BytesIO()
    first_frame.save(
        output,
        format="GIF",
        save_all=True,
        append_images=[second_frame],
    )
    return output.getvalue()


def _assert_webp(image_bytes: bytes) -> None:
    with PillowImage.open(BytesIO(image_bytes)) as image:
        assert image.format == "WEBP"
        assert image.mode == "RGBA"
        assert image.size == (4, 3)


@pytest.mark.parametrize(
    ("handler_name", "service_getter_name"),
    [
        ("create_image_search_session", "get_image_search_service"),
        ("create_plot_image_search_session", "get_movie_plot_image_search_service"),
    ],
)
async def test_image_search_sessions_convert_uploaded_images_to_webp(
    monkeypatch, handler_name, service_getter_name
):
    received: dict[str, bytes] = {}

    class _Service:
        def create_session_and_first_page(self, *, image_bytes, **_kwargs):
            received["image_bytes"] = image_bytes
            return {"status": "ready"}

    monkeypatch.setattr(image_search, service_getter_name, lambda: _Service())
    handler = getattr(image_search, handler_name)
    await handler(
        file=UploadFile(filename="query.png", file=BytesIO(_png_bytes())),
    )

    _assert_webp(received["image_bytes"])


def test_image_search_query_uses_first_frame_of_animated_gif():
    with PillowImage.open(
        BytesIO(normalize_image_search_query(_animated_gif_bytes()))
    ) as image:
        assert image.format == "WEBP"
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize(
    ("image_bytes", "message"),
    [
        (b"", "Uploaded file is empty"),
        (b"not an image", "uploaded image is invalid"),
    ],
)
async def test_image_search_sessions_reject_empty_or_invalid_images(
    image_bytes, message
):
    with pytest.raises(ValueError, match=message):
        await image_search._read_image_search_query(
            UploadFile(filename="query.bin", file=BytesIO(image_bytes))
        )
