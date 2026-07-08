import sys
from typing import Dict

import httpx
import pytest
from PIL import Image as PillowImage

from src.service.catalog.movie_image_service import ImageDownloadError, MovieImageService


class _FakeDownloadResponse:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content


def _patch_fake_cv2(monkeypatch: pytest.MonkeyPatch, column_gradient: list[float]) -> None:
    numpy = pytest.importorskip("numpy")

    class _FakeCv2Module:
        COLOR_BGR2GRAY = 0
        CV_64F = 0

        @staticmethod
        def cvtColor(image, code):
            return image[:, :, 0]

        @staticmethod
        def Sobel(gray, depth, dx, dy, ksize=3):
            height, width = gray.shape
            gradient = numpy.zeros((height, width), dtype=float)
            gradient[:] = numpy.array(column_gradient, dtype=float)
            return gradient

    monkeypatch.setitem(sys.modules, "cv2", _FakeCv2Module())


def test_detect_split_points_rejects_obviously_asymmetric_points(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((8, 200, 3), dtype=float)
    column_gradient = [0.0] * 200
    column_gradient[95] = 10.0
    column_gradient[150] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = MovieImageService._detect_split_points(image)

    assert (left_point, right_point) == (-1, -1)


def test_detect_split_points_accepts_nearly_symmetric_points(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((8, 200, 3), dtype=float)
    column_gradient = [0.0] * 200
    column_gradient[82] = 10.0
    column_gradient[121] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = MovieImageService._detect_split_points(image)

    assert (left_point, right_point) == (82, 121)


def test_detect_split_points_accepts_wide_spine_portrait_crop(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((538, 800, 3), dtype=float)
    column_gradient = [0.0] * 800
    column_gradient[373] = 10.0
    column_gradient[462] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = MovieImageService._detect_split_points(image)

    assert (left_point, right_point) == (373, 462)


def test_detect_split_points_accepts_narrow_spine_portrait_crop(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((538, 800, 3), dtype=float)
    column_gradient = [0.0] * 800
    column_gradient[379] = 10.0
    column_gradient[407] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = MovieImageService._detect_split_points(image)

    assert (left_point, right_point) == (379, 407)


def test_detect_split_points_accepts_center_split_portrait_crop(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((565, 800, 3), dtype=float)
    column_gradient = [0.0] * 800
    column_gradient[300] = 10.0
    column_gradient[400] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = MovieImageService._detect_split_points(image)

    assert (left_point, right_point) == (300, 400)


def test_detect_split_points_rejects_non_portrait_enhanced_crop(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((276, 276, 3), dtype=float)
    column_gradient = [0.0] * 276
    column_gradient[91] = 10.0
    column_gradient[207] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = MovieImageService._detect_split_points(image)

    assert (left_point, right_point) == (-1, -1)


def test_split_image_writes_portrait_crop_for_wide_spine(tmp_path, monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    source_path = tmp_path / "cover.jpg"
    output_path = tmp_path / "thin-cover.jpg"
    source_path.write_bytes(b"cover")
    image = numpy.zeros((538, 800, 3), dtype=numpy.uint8)

    class _FakeCv2Module:
        COLOR_BGR2GRAY = 0
        CV_64F = 0

        @staticmethod
        def imread(path):
            return image.copy() if path == str(source_path) else None

        @staticmethod
        def imwrite(path, cropped_image):
            PillowImage.fromarray(cropped_image).save(path, format="JPEG")
            return True

        @staticmethod
        def cvtColor(source_image, code):
            return source_image[:, :, 0]

        @staticmethod
        def Sobel(gray, depth, dx, dy, ksize=3):
            gradient = numpy.zeros_like(gray, dtype=float)
            gradient[:, 373] = 10.0
            gradient[:, 462] = 12.0
            return gradient

    monkeypatch.setitem(sys.modules, "cv2", _FakeCv2Module())

    assert MovieImageService._split_image(source_path, output_path) is True

    with PillowImage.open(output_path) as output_image:
        assert output_image.size == (338, 538)


def test_download_image_retries_until_success(monkeypatch: pytest.MonkeyPatch, tmp_path):
    service = MovieImageService()
    target_path = tmp_path / "retry.jpg"
    attempts: Dict[str, int] = {"count": 0}

    def _fake_request(method: str, url: str):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.TimeoutException("timeout")
        if attempts["count"] == 2:
            return _FakeDownloadResponse(status_code=503, content=b"")
        return _FakeDownloadResponse(status_code=200, content=b"ok")

    monkeypatch.setattr(service.http_client, "request", _fake_request)
    service._download_image("https://example.com/retry.jpg", target_path)

    assert attempts["count"] == 3
    assert target_path.read_bytes() == b"ok"


def test_download_image_skips_when_target_already_exists(monkeypatch: pytest.MonkeyPatch, tmp_path):
    service = MovieImageService()
    target_path = tmp_path / "exists.jpg"
    target_path.write_bytes(b"cached")

    def _fake_request(method: str, url: str):
        raise AssertionError("http client should not be called for existing files")

    monkeypatch.setattr(service.http_client, "request", _fake_request)

    service._download_image("https://example.com/exists.jpg", target_path)

    assert target_path.read_bytes() == b"cached"


def test_download_image_raises_after_retry_exhausted(monkeypatch: pytest.MonkeyPatch, tmp_path):
    service = MovieImageService()
    target_path = tmp_path / "fail.jpg"

    def _fake_request(method: str, url: str):
        return _FakeDownloadResponse(status_code=500, content=b"")

    monkeypatch.setattr(service.http_client, "request", _fake_request)

    with pytest.raises(ImageDownloadError):
        service._download_image("https://example.com/fail.jpg", target_path)
