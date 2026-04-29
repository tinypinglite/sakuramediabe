import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

from src.common.file_signatures import (
    FILE_SIGNATURE_EXPIRE_SECONDS,
    build_signed_image_url as build_runtime_signed_image_url,
    build_signed_media_url as build_runtime_signed_media_url,
    build_signed_subtitle_url as build_runtime_signed_subtitle_url,
)
from src.model import Movie, Subtitle

TEST_FILE_SIGNATURE_SECRET = "test-file-secret"
TEST_FILE_SIGNATURE_NOW = 1700000000
TEST_FILE_SIGNATURE_EXPIRES = 1700000900


def _build_signature(relative_path: str, expires: int = TEST_FILE_SIGNATURE_EXPIRES) -> str:
    payload = f"images:{relative_path}:{expires}"
    return hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _build_subtitle_signature(subtitle_id: int, expires: int = TEST_FILE_SIGNATURE_EXPIRES) -> str:
    payload = f"subtitles:{subtitle_id}:{expires}"
    return hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _extract_expires(url: str) -> int:
    return int(parse_qs(urlparse(url).query)["expires"][0])


def test_signed_resource_urls_use_fixed_twelve_hour_expiration():
    expected_expires = TEST_FILE_SIGNATURE_NOW + FILE_SIGNATURE_EXPIRE_SECONDS

    # 三类资源签名链接统一使用代码内固定 12 小时有效期。
    assert _extract_expires(build_runtime_signed_image_url("actors/actor-a.jpg")) == expected_expires
    assert _extract_expires(build_runtime_signed_media_url(100)) == expected_expires
    assert _extract_expires(build_runtime_signed_subtitle_url(200)) == expected_expires


def test_file_route_returns_image_when_signature_is_valid(
    client,
    monkeypatch,
    tmp_path,
    build_signed_image_url,
):
    monkeypatch.setattr("src.config.config.settings.media.import_image_root_path", str(tmp_path))
    image_path = tmp_path / "actors" / "actor-a.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"image-bytes")

    response = client.get(build_signed_image_url("actors/actor-a.jpg"))

    assert response.status_code == 200
    assert response.content == b"image-bytes"
    assert response.headers["content-type"] == "image/jpeg"


def test_file_route_returns_movie_cover_when_signature_is_valid(
    client,
    monkeypatch,
    tmp_path,
    build_signed_image_url,
):
    monkeypatch.setattr("src.config.config.settings.media.import_image_root_path", str(tmp_path))
    image_path = tmp_path / "movies" / "SONE-210" / "cover.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"cover-bytes")

    response = client.get(build_signed_image_url("movies/SONE-210/cover.jpg"))

    assert response.status_code == 200
    assert response.content == b"cover-bytes"
    assert response.headers["content-type"] == "image/jpeg"


def test_file_route_rejects_missing_signature(client):
    response = client.get("/files/images/actors/actor-a.jpg")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_invalid"


def test_file_route_rejects_expired_signature(client, monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config.settings.media.import_image_root_path", str(tmp_path))
    image_path = tmp_path / "actors" / "actor-a.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"image-bytes")

    expired_at = TEST_FILE_SIGNATURE_EXPIRES - 901
    signature = _build_signature("actors/actor-a.jpg", expired_at)
    response = client.get(
        "/files/images/actors/actor-a.jpg"
        f"?expires={expired_at}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_expired"


def test_file_route_rejects_signature_reuse_for_different_path(
    client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("src.config.config.settings.media.import_image_root_path", str(tmp_path))
    first_image = tmp_path / "actors" / "actor-a.jpg"
    second_image = tmp_path / "actors" / "actor-b.jpg"
    first_image.parent.mkdir(parents=True, exist_ok=True)
    first_image.write_bytes(b"a")
    second_image.write_bytes(b"b")

    signature = _build_signature("actors/actor-a.jpg")
    response = client.get(
        "/files/images/actors/actor-b.jpg"
        f"?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_invalid"


def test_file_route_rejects_path_traversal(client, monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config.settings.media.import_image_root_path", str(tmp_path))
    traversal_path = "../secret.txt"
    signature = _build_signature(traversal_path)
    response = client.get(
        "/files/images/%2E%2E/secret.txt"
        f"?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_path_invalid"


def test_file_route_returns_404_when_file_is_missing(client, monkeypatch, tmp_path, build_signed_image_url):
    monkeypatch.setattr("src.config.config.settings.media.import_image_root_path", str(tmp_path))

    response = client.get(build_signed_image_url("actors/missing.jpg"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "file_not_found"


def test_subtitle_file_route_returns_subtitle_when_signature_is_valid(
    client,
    monkeypatch,
    tmp_path,
    build_signed_subtitle_url,
):
    monkeypatch.setattr("src.config.config.settings.media.subtitle_root_path", str(tmp_path / "subtitles"))
    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    subtitle_path = tmp_path / "subtitles" / "ABP-123" / "ABP-123.srt"
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_path.write_text("subtitle", encoding="utf-8")
    subtitle = Subtitle.create(movie=movie, file_path=str(subtitle_path))

    response = client.get(build_signed_subtitle_url(subtitle.id))

    assert response.status_code == 200
    assert response.text == "subtitle"
    assert response.headers["content-type"].startswith("text/plain")


def test_subtitle_file_route_rejects_missing_signature(client):
    response = client.get("/files/subtitles/1")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_invalid"


def test_subtitle_file_route_rejects_expired_signature(client, monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config.settings.media.subtitle_root_path", str(tmp_path / "subtitles"))
    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    subtitle_path = tmp_path / "subtitles" / "ABP-123" / "ABP-123.srt"
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_path.write_text("subtitle", encoding="utf-8")
    subtitle = Subtitle.create(movie=movie, file_path=str(subtitle_path))

    expired_at = TEST_FILE_SIGNATURE_EXPIRES - 901
    signature = _build_subtitle_signature(subtitle.id, expired_at)
    response = client.get(
        f"/files/subtitles/{subtitle.id}?expires={expired_at}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_expired"


def test_subtitle_file_route_rejects_signature_reuse_for_different_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config.settings.media.subtitle_root_path", str(tmp_path / "subtitles"))
    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    first_subtitle_path = tmp_path / "subtitles" / "ABP-123" / "ABP-123.srt"
    second_subtitle_path = tmp_path / "subtitles" / "ABP-123" / "ABP-123.zh.srt"
    first_subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    first_subtitle_path.write_text("subtitle", encoding="utf-8")
    second_subtitle_path.write_text("subtitle-zh", encoding="utf-8")
    first_subtitle = Subtitle.create(movie=movie, file_path=str(first_subtitle_path))
    second_subtitle = Subtitle.create(movie=movie, file_path=str(second_subtitle_path))

    signature = _build_subtitle_signature(first_subtitle.id)
    response = client.get(
        f"/files/subtitles/{second_subtitle.id}?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_invalid"


def test_subtitle_file_route_rejects_non_srt_file_path(client, tmp_path):
    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    subtitle_path = tmp_path / "ABP-123.ass"
    subtitle_path.write_text("subtitle", encoding="utf-8")
    subtitle = Subtitle.create(movie=movie, file_path=str(subtitle_path))

    signature = _build_subtitle_signature(subtitle.id)
    response = client.get(
        f"/files/subtitles/{subtitle.id}?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_path_invalid"


def test_subtitle_file_route_rejects_path_outside_allowed_roots(client, tmp_path):
    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    subtitle_path = tmp_path / "outside.srt"
    subtitle_path.write_text("subtitle", encoding="utf-8")
    subtitle = Subtitle.create(movie=movie, file_path=str(subtitle_path))

    signature = _build_subtitle_signature(subtitle.id)
    response = client.get(
        f"/files/subtitles/{subtitle.id}?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_path_invalid"


def test_subtitle_file_route_returns_404_when_subtitle_or_file_is_missing(
    client,
    monkeypatch,
    tmp_path,
    build_signed_subtitle_url,
):
    monkeypatch.setattr("src.config.config.settings.media.subtitle_root_path", str(tmp_path / "subtitles"))
    missing_subtitle_response = client.get(build_signed_subtitle_url(999))
    assert missing_subtitle_response.status_code == 404
    assert missing_subtitle_response.json()["error"]["code"] == "subtitle_not_found"

    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    subtitle = Subtitle.create(movie=movie, file_path=str(tmp_path / "subtitles" / "ABP-123" / "ABP-123.srt"))

    missing_file_response = client.get(build_signed_subtitle_url(subtitle.id))

    assert missing_file_response.status_code == 404
    assert missing_file_response.json()["error"]["code"] == "file_not_found"
