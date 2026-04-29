from pathlib import Path

import pytest

from src.config.config import settings
from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie, MovieSeries
from src.service.discovery.joytag_embedder_client import JoyTagEmbeddingItemError, JoyTagInferenceUnavailableError
from src.service.discovery import ImageSearchIndexService


class _DummyEmbedder:
    def get_runtime_status(self):
        class _Runtime:
            vector_size = 3
        return _Runtime()

    def infer_image_batch(self, image_bytes_list: list[bytes]):
        results = []
        for _image_bytes in image_bytes_list:
            class _Result:
                vector = [0.1, 0.2, 0.3]
            results.append(_Result())
        return results

    def infer_image_bytes(self, _image_bytes: bytes):
        class _Result:
            vector = [0.1, 0.2, 0.3]

        return _Result()


class _DummyStore:
    def __init__(self):
        self.vector_size = None
        self.records = []
        self.batches = []
        self.deleted_media_ids = []
        self.optimize_calls = 0

    def ensure_table(self, vector_size):
        self.vector_size = vector_size

    def upsert_records(self, records):
        batch = list(records)
        self.batches.append(batch)
        self.records.extend(batch)

    def delete_by_media_id(self, media_id):
        self.deleted_media_ids.append(media_id)

    def optimize(self):
        self.optimize_calls += 1
        return {"compacted": True, "optimize_calls": self.optimize_calls}


@pytest.fixture()
def image_index_tables(test_db):
    models = [Image, MovieSeries, Movie, MediaLibrary, Media, MediaThumbnail]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield
    test_db.drop_tables(list(reversed(models)))


def _configure_index_job(
    monkeypatch,
    *,
    index_upsert_batch_size=100,
    optimize_every_records=5000,
    optimize_every_seconds=1800,
    optimize_on_job_end=True,
):
    monkeypatch.setattr(settings.image_search, "index_upsert_batch_size", index_upsert_batch_size)
    monkeypatch.setattr(settings.image_search, "optimize_every_records", optimize_every_records)
    monkeypatch.setattr(settings.image_search, "optimize_every_seconds", optimize_every_seconds)
    monkeypatch.setattr(settings.image_search, "optimize_on_job_end", optimize_on_job_end)
    monkeypatch.setattr(settings.image_search, "inference_batch_size", 2)


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


def test_index_pending_thumbnails_marks_success_with_batch_upsert(
    image_index_tables,
    monkeypatch,
    tmp_path,
):
    first = _create_thumbnail(tmp_path, "ABC-001")
    second = _create_thumbnail(tmp_path, "ABC-002")
    third = _create_thumbnail(tmp_path, "ABC-003")
    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_DummyEmbedder())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))
    _configure_index_job(
        monkeypatch,
        index_upsert_batch_size=2,
        optimize_every_records=5000,
        optimize_every_seconds=999999,
        optimize_on_job_end=False,
    )

    stats = service.index_pending_thumbnails()

    assert stats == {
        "pending_thumbnails": 3,
        "successful_thumbnails": 3,
        "failed_thumbnails": 0,
    }
    assert [len(batch) for batch in store.batches] == [2, 1]
    assert store.vector_size == 3
    assert store.optimize_calls == 0
    for thumbnail in (first, second, third):
        refreshed = MediaThumbnail.get_by_id(thumbnail.id)
        assert refreshed.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS


def test_index_pending_thumbnails_marks_failure_when_image_is_missing(
    image_index_tables,
    monkeypatch,
    tmp_path,
):
    thumbnail = _create_thumbnail(tmp_path, "ABC-004", missing_file=True)
    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_DummyEmbedder())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))
    _configure_index_job(
        monkeypatch,
        index_upsert_batch_size=2,
        optimize_on_job_end=False,
    )

    stats = service.index_pending_thumbnails()

    refreshed = MediaThumbnail.get_by_id(thumbnail.id)
    assert stats["failed_thumbnails"] == 1
    assert stats["successful_thumbnails"] == 0
    assert refreshed.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_FAILED
    assert store.batches == []


def test_index_pending_thumbnails_runs_segment_optimize_by_record_threshold(
    image_index_tables,
    monkeypatch,
    tmp_path,
):
    _create_thumbnail(tmp_path, "ABC-005")
    _create_thumbnail(tmp_path, "ABC-006")
    _create_thumbnail(tmp_path, "ABC-007")
    _create_thumbnail(tmp_path, "ABC-008")
    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_DummyEmbedder())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))
    _configure_index_job(
        monkeypatch,
        index_upsert_batch_size=2,
        optimize_every_records=3,
        optimize_every_seconds=999999,
        optimize_on_job_end=False,
    )

    stats = service.index_pending_thumbnails()

    assert stats["successful_thumbnails"] == 4
    assert stats["failed_thumbnails"] == 0
    assert store.optimize_calls == 1


def test_index_pending_thumbnails_runs_segment_optimize_by_time_threshold(
    image_index_tables,
    monkeypatch,
    tmp_path,
):
    _create_thumbnail(tmp_path, "ABC-009")
    _create_thumbnail(tmp_path, "ABC-010")
    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_DummyEmbedder())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))
    _configure_index_job(
        monkeypatch,
        index_upsert_batch_size=1,
        optimize_every_records=999999,
        optimize_every_seconds=1,
        optimize_on_job_end=False,
    )

    class _Clock:
        def __init__(self):
            self._now = 0

        def __call__(self):
            self._now += 2
            return float(self._now)

    monkeypatch.setattr("src.service.discovery.image_search_index_service.time.time", _Clock())

    stats = service.index_pending_thumbnails()

    assert stats["successful_thumbnails"] == 2
    assert stats["failed_thumbnails"] == 0
    assert store.optimize_calls >= 1


def test_index_pending_thumbnails_flushes_partial_batch_on_interrupt(
    image_index_tables,
    monkeypatch,
    tmp_path,
):
    first = _create_thumbnail(tmp_path, "ABC-011")
    second = _create_thumbnail(tmp_path, "ABC-012")
    third = _create_thumbnail(tmp_path, "ABC-013")
    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_DummyEmbedder())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))
    _configure_index_job(
        monkeypatch,
        index_upsert_batch_size=3,
        optimize_every_records=999999,
        optimize_every_seconds=999999,
        optimize_on_job_end=False,
    )
    original_build_vector_record = service._build_vector_record

    def _build_vector_record_with_interrupt(thumbnail, inference):
        if thumbnail.id == third.id:
            raise KeyboardInterrupt("interrupt-test")
        return original_build_vector_record(thumbnail, inference)

    monkeypatch.setattr(service, "_build_vector_record", _build_vector_record_with_interrupt)

    with pytest.raises(KeyboardInterrupt):
        service.index_pending_thumbnails()

    refreshed_first = MediaThumbnail.get_by_id(first.id)
    refreshed_second = MediaThumbnail.get_by_id(second.id)
    refreshed_third = MediaThumbnail.get_by_id(third.id)
    assert refreshed_first.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS
    assert refreshed_second.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS
    assert refreshed_third.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_PENDING
    assert len(store.records) == 2
    assert [len(batch) for batch in store.batches] == [2]


def test_optimize_index_logs_flow(monkeypatch):
    events: list[tuple[str, str]] = []

    class FakeLogger:
        def info(self, message, *args):
            events.append(("info", message.format(*args) if args else message))

    class DummyStore:
        def ensure_table(self, _vector_size):
            return None

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


def test_index_pending_thumbnails_marks_item_failure_without_aborting_job(
    image_index_tables,
    monkeypatch,
    tmp_path,
):
    first = _create_thumbnail(tmp_path, "ABC-101")
    second = _create_thumbnail(tmp_path, "ABC-102")

    class _BatchEmbedder(_DummyEmbedder):
        def infer_image_batch(self, image_bytes_list: list[bytes]):
            assert len(image_bytes_list) == 2
            return [
                type("_Result", (), {"vector": [0.1, 0.2, 0.3]})(),
                JoyTagEmbeddingItemError(
                    index=1,
                    error_code="invalid_image",
                    error_message="bad image",
                ),
            ]

    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_BatchEmbedder())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))
    _configure_index_job(monkeypatch, index_upsert_batch_size=10, optimize_on_job_end=False)

    stats = service.index_pending_thumbnails()

    assert stats == {
        "pending_thumbnails": 2,
        "successful_thumbnails": 1,
        "failed_thumbnails": 1,
    }
    assert MediaThumbnail.get_by_id(first.id).joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS
    assert MediaThumbnail.get_by_id(second.id).joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_FAILED


def test_index_pending_thumbnails_raises_when_remote_batch_is_unavailable(
    image_index_tables,
    monkeypatch,
    tmp_path,
):
    first = _create_thumbnail(tmp_path, "ABC-201")
    second = _create_thumbnail(tmp_path, "ABC-202")

    class _UnavailableEmbedder(_DummyEmbedder):
        def infer_image_batch(self, _image_bytes_list: list[bytes]):
            raise JoyTagInferenceUnavailableError("service unavailable")

    store = _DummyStore()
    service = ImageSearchIndexService(store=store, embedder=_UnavailableEmbedder())
    monkeypatch.setattr("src.common.file_signatures.settings.media.import_image_root_path", str(tmp_path))
    _configure_index_job(monkeypatch, index_upsert_batch_size=10, optimize_on_job_end=False)

    with pytest.raises(JoyTagInferenceUnavailableError):
        service.index_pending_thumbnails()

    assert MediaThumbnail.get_by_id(first.id).joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_PENDING
    assert MediaThumbnail.get_by_id(second.id).joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_PENDING
    assert store.records == []
