from io import BytesIO

import numpy as np
from PIL import Image

from src.service.catalog.movie_image_service import MovieImageService


def _symmetric_spine_image() -> Image.Image:
    # 中间左右各一条强竖边，模拟合订封面的书脊两侧。
    array = np.full((300, 200, 3), 128, dtype=np.uint8)
    array[:, 80:82] = 255
    array[:, 120:122] = 0
    return Image.fromarray(array, "RGB")


def _image_bytes(image: Image.Image, image_format: str = "PNG") -> bytes:
    buffer = BytesIO()
    save_kwargs = {"quality": 95} if image_format == "JPEG" else {}
    image.save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue()


def test_detect_split_points_finds_symmetric_spine_edges():
    assert MovieImageService._detect_split_points(_symmetric_spine_image()) == (79, 119)


def test_detect_split_points_ignores_flat_image():
    flat = Image.new("RGB", (200, 300), (128, 128, 128))

    assert MovieImageService._detect_split_points(flat) == (-1, -1)


def test_detect_split_points_handles_jpeg_artifacts():
    # 真实封面多为 JPEG，压缩伪影会让切点轻微偏移；允许 ±2 列。
    data = _image_bytes(_symmetric_spine_image(), "JPEG")
    with Image.open(BytesIO(data)) as decoded:
        left, right = MovieImageService._detect_split_points(decoded.convert("RGB"))

    assert left == 79
    assert 119 <= right <= 122


def test_split_image_crops_right_side_and_writes_png(tmp_path):
    output_path = tmp_path / "thin-cover.png"

    result = MovieImageService._split_image(
        _image_bytes(_symmetric_spine_image()), output_path
    )

    assert result is True
    with Image.open(output_path) as cropped:
        assert cropped.size == (200 - 119, 300)
        assert cropped.mode == "RGB"


def test_split_image_writes_jpeg(tmp_path):
    output_path = tmp_path / "thin-cover.jpg"

    result = MovieImageService._split_image(
        _image_bytes(_symmetric_spine_image(), "JPEG"), output_path
    )

    assert result is True
    with Image.open(output_path) as cropped:
        assert cropped.format == "JPEG"
        assert cropped.width < 200
        assert cropped.height == 300


def test_split_image_returns_false_for_invalid_bytes(tmp_path):
    output_path = tmp_path / "thin-cover.png"

    result = MovieImageService._split_image(b"not an image", output_path)

    assert result is False
    assert not output_path.exists()


def test_split_image_returns_false_when_no_split_point_is_found(tmp_path):
    output_path = tmp_path / "thin-cover.png"
    flat = Image.new("RGB", (200, 300), (128, 128, 128))

    result = MovieImageService._split_image(_image_bytes(flat), output_path)

    assert result is False
    assert not output_path.exists()
