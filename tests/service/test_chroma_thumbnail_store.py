import pytest

from src.service.discovery.chroma_thumbnail_store import (
    ChromaThumbnailStore,
    ThumbnailVectorRecord,
)


class _FakeCollection:
    def __init__(self, name: str, *, metadata: dict | None = None):
        self.name = name
        self.metadata = metadata or {}
        self._records: dict[str, dict] = {}
        self.queries: list[dict] = []
        self.deletes: list[dict] = []
        self.upserts: list[dict] = []

    def upsert(self, ids, embeddings, metadatas):
        self.upserts.append({"ids": list(ids), "embeddings": list(embeddings), "metadatas": list(metadatas)})
        for record_id, embedding, metadata in zip(ids, embeddings, metadatas):
            self._records[record_id] = {
                "id": record_id,
                "embedding": list(embedding),
                "metadata": dict(metadata),
            }

    def delete(self, ids=None, where=None):
        self.deletes.append({"ids": list(ids) if ids else None, "where": where})
        if ids:
            for record_id in ids:
                self._records.pop(record_id, None)
        if where:
            for record_id, record in list(self._records.items()):
                if self._match_where(record["metadata"], where):
                    self._records.pop(record_id, None)

    def query(self, query_embeddings, n_results, where=None, include=None):
        self.queries.append({"n_results": n_results, "where": where, "include": include})
        candidates = list(self._records.values())
        if where is not None:
            candidates = [item for item in candidates if self._match_where(item["metadata"], where)]
        # 按插入顺序返回 ID — 上层只关心顺序，距离用占位值
        selected = candidates[:n_results]
        return {
            "ids": [[item["id"] for item in selected]],
            "metadatas": [[item["metadata"] for item in selected]],
            "distances": [[float(index) * 0.1 for index, _ in enumerate(selected)]],
        }

    def count(self):
        return len(self._records)

    def peek(self, limit: int = 10):
        ordered = list(self._records.values())[:limit]
        return {
            "ids": [item["id"] for item in ordered],
            "embeddings": [list(item["embedding"]) for item in ordered],
            "metadatas": [dict(item["metadata"]) for item in ordered],
        }

    @classmethod
    def _match_where(cls, metadata: dict, where: dict) -> bool:
        if "$and" in where:
            return all(cls._match_where(metadata, clause) for clause in where["$and"])
        if "$or" in where:
            return any(cls._match_where(metadata, clause) for clause in where["$or"])
        for key, condition in where.items():
            actual = metadata.get(key)
            if isinstance(condition, dict):
                for operator, value in condition.items():
                    if operator == "$eq" and actual != value:
                        return False
                    if operator == "$ne" and actual == value:
                        return False
                    if operator == "$in" and actual not in value:
                        return False
                    if operator == "$nin" and actual in value:
                        return False
            else:
                if actual != condition:
                    return False
        return True


class _FakeClient:
    def __init__(self):
        self.collections: dict[str, _FakeCollection] = {}

    def get_or_create_collection(self, name, metadata=None):
        if name not in self.collections:
            self.collections[name] = _FakeCollection(name, metadata=metadata)
        return self.collections[name]

    def list_collections(self):
        return list(self.collections.values())


@pytest.fixture()
def fake_client():
    return _FakeClient()


@pytest.fixture()
def store(fake_client):
    return ChromaThumbnailStore(uri="/tmp/chroma", collection_name="thumbs", client=fake_client)


def _record(thumbnail_id: int, *, media_id: int = 10, movie_id: int = 100, offset: int = 12, vector=None):
    return ThumbnailVectorRecord(
        thumbnail_id=thumbnail_id,
        media_id=media_id,
        movie_id=movie_id,
        offset_seconds=offset,
        vector=vector or [0.1, 0.2, 0.3],
    )


def test_ensure_collection_creates_collection_with_cosine_metric(store, fake_client):
    store.ensure_collection(vector_size=3)

    collection = fake_client.collections["thumbs"]
    assert collection.metadata.get("hnsw:space") == "cosine"


def test_ensure_collection_rejects_non_positive_vector_size(store):
    with pytest.raises(ValueError):
        store.ensure_collection(vector_size=0)


def test_upsert_records_writes_string_ids_and_metadata(store, fake_client):
    store.upsert_records([_record(1), _record(2)])

    collection = fake_client.collections["thumbs"]
    assert collection.upserts[0]["ids"] == ["1", "2"]
    assert collection.upserts[0]["metadatas"][0] == {
        "thumbnail_id": 1,
        "media_id": 10,
        "movie_id": 100,
        "offset_seconds": 12,
    }
    assert isinstance(collection.upserts[0]["embeddings"][0][0], float)


def test_upsert_records_with_empty_input_is_a_noop(store, fake_client):
    store.upsert_records([])

    assert fake_client.collections == {}


def test_delete_by_thumbnail_ids_normalizes_to_string_ids(store, fake_client):
    store.upsert_records([_record(1), _record(2), _record(3)])

    store.delete_by_thumbnail_ids([1, 1, 3])

    collection = fake_client.collections["thumbs"]
    assert collection.deletes[-1]["ids"] == ["1", "3"]


def test_delete_by_media_id_uses_where_filter(store, fake_client):
    store.upsert_records([_record(1, media_id=10), _record(2, media_id=20)])

    store.delete_by_media_id(20)

    collection = fake_client.collections["thumbs"]
    assert collection.deletes[-1]["where"] == {"media_id": 20}
    # 媒体 20 的记录应被清除
    remaining_ids = sorted(collection._records.keys())
    assert remaining_ids == ["1"]


def test_search_passes_n_results_equal_to_limit_plus_offset(store, fake_client):
    store.upsert_records(
        [
            _record(1, movie_id=101),
            _record(2, movie_id=102),
            _record(3, movie_id=103),
        ]
    )

    hits = store.search(query_vector=[0.1, 0.2, 0.3], limit=2, offset=1)

    collection = fake_client.collections["thumbs"]
    assert collection.queries[-1]["n_results"] == 3
    assert [hit.thumbnail_id for hit in hits] == [2, 3]


def test_search_with_movie_ids_builds_in_filter(store, fake_client):
    store.upsert_records(
        [
            _record(1, movie_id=101),
            _record(2, movie_id=102),
        ]
    )

    store.search(query_vector=[0.1, 0.2, 0.3], limit=10, movie_ids=[101, 102])

    collection = fake_client.collections["thumbs"]
    assert collection.queries[-1]["where"] == {"movie_id": {"$in": [101, 102]}}


def test_search_with_exclude_movie_ids_builds_nin_filter(store, fake_client):
    store.upsert_records([_record(1, movie_id=101)])

    store.search(query_vector=[0.1, 0.2, 0.3], limit=10, exclude_movie_ids=[999])

    collection = fake_client.collections["thumbs"]
    assert collection.queries[-1]["where"] == {"movie_id": {"$nin": [999]}}


def test_search_combines_both_filters_with_and(store, fake_client):
    store.upsert_records([_record(1, movie_id=101)])

    store.search(
        query_vector=[0.1, 0.2, 0.3],
        limit=10,
        movie_ids=[101, 102],
        exclude_movie_ids=[103],
    )

    collection = fake_client.collections["thumbs"]
    assert collection.queries[-1]["where"] == {
        "$and": [
            {"movie_id": {"$in": [101, 102]}},
            {"movie_id": {"$nin": [103]}},
        ]
    }


def test_search_rejects_invalid_arguments(store):
    with pytest.raises(ValueError):
        store.search(query_vector=[0.1], limit=0)
    with pytest.raises(ValueError):
        store.search(query_vector=[0.1], limit=1, offset=-1)


@pytest.mark.parametrize(
    ("distance", "expected_score"),
    [
        (0.0, 1.0),
        (1.0, 0.5),
        (2.0, 0.0),
    ],
)
def test_parse_hit_maps_cosine_distance_to_zero_one_score(distance, expected_score):
    hit = ChromaThumbnailStore._parse_hit(
        {"thumbnail_id": 1, "media_id": 2, "movie_id": 3, "offset_seconds": 4},
        distance,
    )

    assert hit.score == expected_score


def test_inspect_status_reports_collection_details(store, fake_client):
    store.upsert_records([_record(1, vector=[0.1, 0.2, 0.3, 0.4])])

    status = store.inspect_status()

    assert status["healthy"] is True
    assert status["uri"] == "/tmp/chroma"
    assert status["collection_name"] == "thumbs"
    assert status["collection_exists"] is True
    assert status["row_count"] == 1
    assert status["vector_size"] == 4
    assert status["error"] is None


def test_inspect_status_handles_missing_collection_without_error(store):
    status = store.inspect_status()

    assert status["healthy"] is True
    assert status["collection_exists"] is False
    assert status["row_count"] is None
    assert status["vector_size"] is None
    assert status["error"] is None
