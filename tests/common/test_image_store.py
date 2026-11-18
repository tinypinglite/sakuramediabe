import zipfile
from pathlib import Path

import pytest

from src.common.image_store import image_pack_path, read_image_bytes, write_pack
from src.config.config import settings


def _use_image_root(monkeypatch, tmp_path: Path) -> Path:
    image_root = tmp_path / "assets"
    image_root.mkdir()
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    return image_root


def test_read_image_bytes_reads_plain_file(monkeypatch, tmp_path):
    image_root = _use_image_root(monkeypatch, tmp_path)
    relative_path = "videos/302/cover/0.webp"
    target = image_root / relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cover-bytes")

    assert image_pack_path(relative_path) is None
    assert read_image_bytes(relative_path) == b"cover-bytes"


def test_read_image_bytes_prefers_pack_entry(monkeypatch, tmp_path):
    image_root = _use_image_root(monkeypatch, tmp_path)
    relative_path = "movies/aa/AAA-001/media/7/thumbnails/10.webp"
    thumbnails_dir = image_root / Path(relative_path).parent
    thumbnails_dir.mkdir(parents=True)
    (thumbnails_dir / "10.webp").write_bytes(b"legacy-bytes")
    pack_path = thumbnails_dir.with_name("thumbnails.zip")
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("10.webp", b"packed-bytes")

    assert image_pack_path(relative_path) == pack_path
    assert read_image_bytes(relative_path) == b"packed-bytes"


def test_read_image_bytes_falls_back_to_file_when_entry_missing(monkeypatch, tmp_path):
    image_root = _use_image_root(monkeypatch, tmp_path)
    relative_path = "movies/aa/AAA-001/media/7/thumbnails/10.webp"
    thumbnails_dir = image_root / Path(relative_path).parent
    thumbnails_dir.mkdir(parents=True)
    (thumbnails_dir / "10.webp").write_bytes(b"legacy-bytes")
    pack_path = thumbnails_dir.with_name("thumbnails.zip")
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("20.webp", b"other-entry")

    assert read_image_bytes(relative_path) == b"legacy-bytes"


def test_read_image_bytes_missing_entry_and_file_raises(monkeypatch, tmp_path):
    image_root = _use_image_root(monkeypatch, tmp_path)
    relative_path = "movies/aa/AAA-001/media/7/thumbnails/10.webp"
    thumbnails_dir = image_root / Path(relative_path).parent
    thumbnails_dir.mkdir(parents=True)
    pack_path = thumbnails_dir.with_name("thumbnails.zip")
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("20.webp", b"other-entry")

    with pytest.raises(FileNotFoundError):
        read_image_bytes(relative_path)


def test_read_image_bytes_corrupt_pack_falls_back_to_file(monkeypatch, tmp_path):
    image_root = _use_image_root(monkeypatch, tmp_path)
    relative_path = "movies/aa/AAA-001/media/7/thumbnails/10.webp"
    thumbnails_dir = image_root / Path(relative_path).parent
    thumbnails_dir.mkdir(parents=True)
    (thumbnails_dir / "10.webp").write_bytes(b"legacy-bytes")
    (thumbnails_dir.with_name("thumbnails.zip")).write_bytes(b"not-a-zip")

    assert read_image_bytes(relative_path) == b"legacy-bytes"


def test_movie_assets_pack_resolution_and_read(monkeypatch, tmp_path):
    image_root = _use_image_root(monkeypatch, tmp_path)
    relative_path = "movies/ab/AAA-001/cover.jpg"
    movie_dir = image_root / "movies" / "ab" / "AAA-001"
    movie_dir.mkdir(parents=True)
    pack_path = movie_dir / "assets.zip"
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("cover.jpg", b"packed-cover")

    assert image_pack_path(relative_path) == pack_path
    assert read_image_bytes(relative_path) == b"packed-cover"


def test_subtitle_paths_have_no_pack(monkeypatch, tmp_path):
    _use_image_root(monkeypatch, tmp_path)

    assert image_pack_path("movies/ab/AAA-001/subtitles/AAA-001-1.srt") is None


def test_write_pack_stores_entries_without_compression(tmp_path):
    source = tmp_path / "0.webp"
    source.write_bytes(b"file-entry-bytes")
    pack_path = tmp_path / "nested" / "thumbnails.zip"

    write_pack(pack_path, [("0.webp", source), ("10.webp", b"bytes-entry")])

    with zipfile.ZipFile(pack_path) as archive:
        assert sorted(archive.namelist()) == ["0.webp", "10.webp"]
        assert archive.read("0.webp") == b"file-entry-bytes"
        assert archive.read("10.webp") == b"bytes-entry"
        assert archive.getinfo("0.webp").compress_type == zipfile.ZIP_STORED
