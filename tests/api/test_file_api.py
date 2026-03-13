import hashlib
import hmac

from src.model import Media, Movie

TEST_FILE_SIGNATURE_SECRET = "test-file-secret"
TEST_FILE_SIGNATURE_EXPIRES = 1700000900


def _build_signature(relative_path: str, expires: int = TEST_FILE_SIGNATURE_EXPIRES) -> str:
    payload = f"images:{relative_path}:{expires}"
    return hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _build_subtitle_signature(media_id: int, file_name: str, expires: int = TEST_FILE_SIGNATURE_EXPIRES) -> str:
    payload = f"subtitles:{media_id}:{file_name}:{expires}"
    return hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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
    tmp_path,
    build_signed_subtitle_url,
):
    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    version_dir = tmp_path / "ABP-123" / "1730000000000"
    version_dir.mkdir(parents=True)
    video_path = version_dir / "ABP-123.mp4"
    video_path.write_bytes(b"video")
    subtitle_path = version_dir / "ABP-123.srt"
    subtitle_path.write_text("subtitle", encoding="utf-8")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

    response = client.get(build_signed_subtitle_url(media.id, "ABP-123.srt"))

    assert response.status_code == 200
    assert response.text == "subtitle"
    assert response.headers["content-type"].startswith("text/plain")


def test_subtitle_file_route_rejects_missing_signature(client):
    response = client.get("/files/subtitles/1/ABP-123.srt")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_invalid"


def test_subtitle_file_route_rejects_expired_signature(client, tmp_path):
    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    version_dir = tmp_path / "ABP-123" / "1730000000000"
    version_dir.mkdir(parents=True)
    video_path = version_dir / "ABP-123.mp4"
    video_path.write_bytes(b"video")
    (version_dir / "ABP-123.srt").write_text("subtitle", encoding="utf-8")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

    expired_at = TEST_FILE_SIGNATURE_EXPIRES - 901
    signature = _build_subtitle_signature(media.id, "ABP-123.srt", expired_at)
    response = client.get(
        f"/files/subtitles/{media.id}/ABP-123.srt"
        f"?expires={expired_at}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_expired"


def test_subtitle_file_route_rejects_signature_reuse_for_different_file(client, tmp_path):
    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    version_dir = tmp_path / "ABP-123" / "1730000000000"
    version_dir.mkdir(parents=True)
    video_path = version_dir / "ABP-123.mp4"
    video_path.write_bytes(b"video")
    (version_dir / "ABP-123.srt").write_text("subtitle", encoding="utf-8")
    (version_dir / "ABP-123.zh.srt").write_text("subtitle-zh", encoding="utf-8")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

    signature = _build_subtitle_signature(media.id, "ABP-123.srt")
    response = client.get(
        f"/files/subtitles/{media.id}/ABP-123.zh.srt"
        f"?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_signature_invalid"


def test_subtitle_file_route_rejects_path_traversal(client):
    signature = _build_subtitle_signature(1, "..\\secret.srt")
    response = client.get(
        "/files/subtitles/1/..%5Csecret.srt"
        f"?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_path_invalid"


def test_subtitle_file_route_rejects_non_srt_file(client, tmp_path):
    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    version_dir = tmp_path / "ABP-123" / "1730000000000"
    version_dir.mkdir(parents=True)
    video_path = version_dir / "ABP-123.mp4"
    video_path.write_bytes(b"video")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

    signature = _build_subtitle_signature(media.id, "ABP-123.ass")
    response = client.get(
        f"/files/subtitles/{media.id}/ABP-123.ass"
        f"?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={signature}"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "file_path_invalid"


def test_subtitle_file_route_returns_404_when_media_or_file_is_missing(
    client,
    tmp_path,
    build_signed_subtitle_url,
):
    missing_media_response = client.get(build_signed_subtitle_url(999, "ABP-123.srt"))
    assert missing_media_response.status_code == 404
    assert missing_media_response.json()["error"]["code"] == "media_not_found"

    movie = Movie.create(movie_number="ABP-123", javdb_id="movie-a", title="Movie A")
    version_dir = tmp_path / "ABP-123" / "1730000000000"
    version_dir.mkdir(parents=True)
    video_path = version_dir / "ABP-123.mp4"
    video_path.write_bytes(b"video")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

    missing_file_response = client.get(build_signed_subtitle_url(media.id, "ABP-123.srt"))

    assert missing_file_response.status_code == 404
    assert missing_file_response.json()["error"]["code"] == "file_not_found"
