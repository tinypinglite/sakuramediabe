from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models

from src.service.discovery.qdrant_thumbnail_store import (
    QdrantThumbnailStore,
    ThumbnailVectorRecord,
)


def _store() -> QdrantThumbnailStore:
    return QdrantThumbnailStore(client=QdrantClient(":memory:"))


def _record(thumbnail_id: int, media_id: int, movie_id: int, vector: list[float]) -> ThumbnailVectorRecord:
    return ThumbnailVectorRecord(
        thumbnail_id=thumbnail_id,
        media_id=media_id,
        movie_id=movie_id,
        offset_seconds=thumbnail_id * 10,
        vector=vector,
    )


def test_ensure_table_creates_missing_collection_and_is_idempotent():
    store = _store()

    store.ensure_table(3)
    store.ensure_table(3)

    status = store.inspect_status()
    assert status["healthy"] is True
    assert status["exists"] is True
    assert status["collection_name"] == QdrantThumbnailStore.COLLECTION_NAME
    collection = store._get_client().get_collection(QdrantThumbnailStore.COLLECTION_NAME)
    assert collection.config.params.vectors.on_disk is True
    assert status["vector_size"] == 3
    assert status["vector_dtype"] == "float16"


def test_ensure_table_uses_low_memory_collection_profile():
    class _FakeClient:
        def __init__(self):
            self.created_kwargs = None

        def collection_exists(self, _collection_name):
            return False

        def create_collection(self, **kwargs):
            self.created_kwargs = kwargs

    client = _FakeClient()
    store = QdrantThumbnailStore(client=client)

    store.ensure_table(768)

    vector_params = client.created_kwargs["vectors_config"]
    hnsw_config = client.created_kwargs["hnsw_config"]
    assert vector_params.size == 768
    assert vector_params.datatype == models.Datatype.FLOAT16
    assert vector_params.on_disk is True
    assert hnsw_config.m == QdrantThumbnailStore.HNSW_M
    assert hnsw_config.ef_construct == QdrantThumbnailStore.HNSW_EF_CONSTRUCT
    assert hnsw_config.on_disk is True
    assert client.created_kwargs["on_disk_payload"] is True


def test_ensure_table_rejects_vector_size_mismatch():
    store = _store()
    store.ensure_table(3)

    with pytest.raises(ValueError, match="vector size mismatch"):
        store.ensure_table(4)


def test_ensure_table_rejects_vector_dtype_mismatch():
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=QdrantThumbnailStore.COLLECTION_NAME,
        vectors_config=models.VectorParams(
            size=3,
            distance=models.Distance.COSINE,
            datatype=models.Datatype.FLOAT32,
        ),
    )
    store = QdrantThumbnailStore(client=client)

    with pytest.raises(ValueError, match="vector dtype mismatch"):
        store.ensure_table(3)


def test_upsert_records_inserts_and_replaces_points():
    store = _store()

    store.upsert_records([_record(1, 10, 100, [1.0, 0.0, 0.0])])
    store.upsert_records([_record(1, 11, 101, [0.0, 1.0, 0.0])])

    hits = store.search([0.0, 1.0, 0.0], limit=5)
    assert len(hits) == 1
    assert hits[0].thumbnail_id == 1
    assert hits[0].media_id == 11
    assert hits[0].movie_id == 101
    assert hits[0].score == pytest.approx(1.0)


def test_delete_by_thumbnail_ids_removes_selected_points():
    store = _store()
    store.upsert_records(
        [
            _record(1, 10, 100, [1.0, 0.0, 0.0]),
            _record(2, 20, 200, [0.0, 1.0, 0.0]),
        ]
    )

    store.delete_by_thumbnail_ids([1])

    hits = store.search([1.0, 0.0, 0.0], limit=5)
    assert [item.thumbnail_id for item in hits] == [2]


def test_delete_by_media_id_removes_matching_points():
    store = _store()
    store.upsert_records(
        [
            _record(1, 10, 100, [1.0, 0.0, 0.0]),
            _record(2, 20, 200, [0.0, 1.0, 0.0]),
            _record(3, 10, 300, [0.0, 0.0, 1.0]),
        ]
    )

    store.delete_by_media_id(10)

    hits = store.search([0.0, 1.0, 0.0], limit=5)
    assert [item.thumbnail_id for item in hits] == [2]


def test_search_applies_movie_filters_and_offset():
    store = _store()
    store.upsert_records(
        [
            _record(1, 10, 100, [1.0, 0.0, 0.0]),
            _record(2, 20, 200, [0.9, 0.1, 0.0]),
            _record(3, 30, 300, [0.8, 0.2, 0.0]),
        ]
    )

    included = store.search([1.0, 0.0, 0.0], limit=10, movie_ids=[100, 300])
    excluded = store.search([1.0, 0.0, 0.0], limit=10, exclude_movie_ids=[100])
    paged = store.search([1.0, 0.0, 0.0], limit=1, offset=1)

    assert [item.movie_id for item in included] == [100, 300]
    assert [item.movie_id for item in excluded] == [200, 300]
    assert [item.thumbnail_id for item in paged] == [2]


def test_search_returns_empty_when_collection_is_missing():
    store = _store()

    assert store.search([1.0, 0.0, 0.0], limit=5) == []


def test_search_validates_pagination_arguments():
    store = _store()

    with pytest.raises(ValueError, match="limit must be positive"):
        store.search([1.0], limit=0)
    with pytest.raises(ValueError, match="offset must be non-negative"):
        store.search([1.0], offset=-1)


def test_parse_hit_normalizes_cosine_score():
    hit = QdrantThumbnailStore._parse_hit(
        SimpleNamespace(
            id=42,
            score=-0.5,
            payload={"media_id": 1, "movie_id": 2, "offset_seconds": 3},
        )
    )

    assert hit.thumbnail_id == 42
    assert hit.score == pytest.approx(0.25)


def test_inspect_status_reports_missing_collection_as_healthy():
    store = _store()

    status = store.inspect_status()

    assert status["healthy"] is True
    assert status["exists"] is False
    assert status["error"] is None


def test_inspect_status_reports_client_error_as_unhealthy():
    class _FailingClient:
        def collection_exists(self, _collection_name):
            raise RuntimeError("qdrant unavailable")

    store = QdrantThumbnailStore(client=_FailingClient())

    status = store.inspect_status()

    assert status["healthy"] is False
    assert status["exists"] is False
    assert "qdrant unavailable" in status["error"]
