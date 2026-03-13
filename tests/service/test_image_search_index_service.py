from pathlib import Path

import pytest

from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie
from src.service.discovery import ImageSearchIndexService


class _DummyEmbedder:
    vector_size = 3

    def infer_image_bytes(self, _image_bytes: bytes):
        class _Result:
            vector = [0.1, 0.2, 0.3]

        return _Result()


class _DummyStore:
    def __init__(self):
        self.vector_size = None
        self.records = []
        self.deleted_media_ids = []

    def ensure_table(self, vector_size):
        self.vector_size = vector_size

    def upsert_records(self, records):
        self.records.extend(records)

    def delete_by_media_id(self, media_id):
        self.deleted_media_ids.append(media_id)


@pytest.fixture()
def image_index_tables(test_db):
    models = [Image, Movie, MediaLibrary, Media, MediaThumbnail]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield
    test_db.drop_tables(list(reversed(models)))


def _create_thumbnail(tmp_path: Path, movie_number: str, *, missing_file: bool = False):
    movie = Movie.create(javdb_id=f"javdb-{movie_number}", movie_number=movie_number, title=movie_number)
    media = Media.create(movie=movie, path=f"/library/{movie_number}.mp4", valid=True)
    relative_path = f"movies/{movie_number}/thumb.webp"
    if not missing_file:
        absolute_path = tmp_path / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(b"image")
    image = Image.create(
        origin=relative_path,
        small=relative_path,
        medium=relative_path,
        large=relative_path,
    )
    return MediaThumbnail.create(media=media, image=image, offset=15)


def test_index_pending_thumbnails_marks_success(image_index_tables, monkeypatch, tmp_path):
    thumbnail = _create_thumbnail(tmp_path, "ABC-001")
    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_DummyEmbedder())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))

    stats = service.index_pending_thumbnails()

    refreshed = MediaThumbnail.get_by_id(thumbnail.id)
    assert stats == {
        "pending_thumbnails": 1,
        "successful_thumbnails": 1,
        "failed_thumbnails": 0,
    }
    assert store.vector_size == 3
    assert store.records[0].thumbnail_id == thumbnail.id
    assert refreshed.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS


def test_index_pending_thumbnails_marks_failure_when_image_is_missing(image_index_tables, monkeypatch, tmp_path):
    thumbnail = _create_thumbnail(tmp_path, "ABC-002", missing_file=True)
    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_DummyEmbedder())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))

    stats = service.index_pending_thumbnails()

    refreshed = MediaThumbnail.get_by_id(thumbnail.id)
    assert stats["failed_thumbnails"] == 1
    assert refreshed.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_FAILED


def test_index_pending_thumbnails_logs_flow(image_index_tables, monkeypatch, tmp_path):
    success_thumbnail = _create_thumbnail(tmp_path, "ABC-003")
    failed_thumbnail = _create_thumbnail(tmp_path, "ABC-004", missing_file=True)
    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_DummyEmbedder())
    events: list[tuple[str, str]] = []

    class FakeLogger:
        def info(self, message, *args):
            events.append(("info", message.format(*args) if args else message))

        def warning(self, message, *args):
            events.append(("warning", message.format(*args) if args else message))

    monkeypatch.setattr("src.service.discovery.image_search_index_service.logger", FakeLogger())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))

    stats = service.index_pending_thumbnails()

    assert stats == {
        "pending_thumbnails": 2,
        "successful_thumbnails": 1,
        "failed_thumbnails": 1,
    }
    assert any(
        level == "info" and "Starting JoyTag thumbnail indexing pending_thumbnails=2" in message
        for level, message in events
    )
    assert any(
        level == "info"
        and f"Indexing JoyTag thumbnail progress=1/2 thumbnail_id={success_thumbnail.id}" in message
        for level, message in events
    )
    assert any(
        level == "info"
        and f"Indexed JoyTag thumbnail thumbnail_id={success_thumbnail.id} media_id={success_thumbnail.media_id}"
        in message
        for level, message in events
    )
    assert any(
        level == "warning"
        and f"JoyTag thumbnail indexing failed thumbnail_id={failed_thumbnail.id} media_id={failed_thumbnail.media_id}"
        in message
        for level, message in events
    )
    assert any(
        level == "info"
        and "Finished JoyTag thumbnail indexing pending_thumbnails=2 successful_thumbnails=1 failed_thumbnails=1"
        in message
        for level, message in events
    )


def test_optimize_index_logs_flow(monkeypatch):
    events: list[tuple[str, str]] = []

    class FakeLogger:
        def info(self, message, *args):
            events.append(("info", message.format(*args) if args else message))

    class DummyStore:
        def optimize(self):
            return {"compacted": True, "indexed_rows": 3}

    monkeypatch.setattr("src.service.discovery.image_search_index_service.logger", FakeLogger())
    service = ImageSearchIndexService(store=DummyStore(), embedder=_DummyEmbedder())

    result = service.optimize_index()

    assert result == {"compacted": True, "indexed_rows": 3}
    assert any(
        level == "info" and "Starting JoyTag index optimization" in message
        for level, message in events
    )
    assert any(
        level == "info"
        and "Finished JoyTag index optimization compacted=True indexed_rows=3" in message
        for level, message in events
    )
