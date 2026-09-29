import zipfile
from pathlib import Path

import pytest

from src.common.image_store import read_image_bytes, thumbnail_pack_path, write_pack
from src.config.config import settings


def _use_image_root(monkeypatch, tmp_path: Path) -> Path:
    image_root = tmp_path / "assets"
    image_root.mkdir()
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    return image_root


def test_read_image_bytes_reads_plain_file(monkeypatch, tmp_path):
    image_root = _use_image_root(monkeypatch, tmp_path)
    target = image_root / "movies" / "aa" / "AAA-001" / "cover.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cover-bytes")

    assert thumbnail_pack_path("movies/aa/AAA-001/cover.jpg") is None
    assert read_image_bytes("movies/aa/AAA-001/cover.jpg") == b"cover-bytes"


def test_read_image_bytes_prefers_pack_entry(monkeypatch, tmp_path):
    image_root = _use_image_root(monkeypatch, tmp_path)
    relative_path = "movies/aa/AAA-001/media/7/thumbnails/10.webp"
    thumbnails_dir = image_root / Path(relative_path).parent
    thumbnails_dir.mkdir(parents=True)
    (thumbnails_dir / "10.webp").write_bytes(b"legacy-bytes")
    pack_path = thumbnails_dir.with_name("thumbnails.zip")
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("10.webp", b"packed-bytes")

    assert thumbnail_pack_path(relative_path) == pack_path
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
