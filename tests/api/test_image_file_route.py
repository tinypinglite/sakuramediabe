from src.common import build_signed_image_url
from src.config.config import settings


def test_image_file_route_sets_long_lived_cache_control(
    client, monkeypatch, tmp_path
):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(
        settings.media, "import_image_root_path", str(image_root)
    )
    target = image_root / "movies" / "aa" / "AAA-001" / "cover.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake-image-bytes")

    response = client.get(build_signed_image_url("movies/aa/AAA-001/cover.jpg"))

    assert response.status_code == 200
    assert response.content == b"fake-image-bytes"
    assert (
        response.headers["cache-control"]
        == "public, max-age=2592000, immutable"
    )
