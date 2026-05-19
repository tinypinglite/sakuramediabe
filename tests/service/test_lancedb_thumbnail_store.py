from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from src.service.discovery.lancedb_thumbnail_store import (
    LanceDbThumbnailStore,
    ThumbnailVectorRecord,
)


class _FakeSearchQuery:
    def __init__(self, rows):
        self.rows = rows
        self.where_expr = None
        self.limit_value = None

    def where(self, expr, prefilter=True):
        self.where_expr = (expr, prefilter)
        return self

    def limit(self, limit):
        self.limit_value = limit
        return self

    def to_list(self):
        if self.limit_value is None:
            return list(self.rows)
        return list(self.rows[:self.limit_value])


class _FakeSchema:
    def __init__(self, vector_size, value_type="float32"):
        self.vector_size = vector_size
        self.value_type = value_type

    def field(self, name):
        assert name == "vector"
        return SimpleNamespace(type=SimpleNamespace(list_size=self.vector_size, value_type=self.value_type))


class _FakeTable:
    def __init__(self, rows=None, vector_size=3, value_type="float16"):
        self.rows = rows or []
        self.schema = _FakeSchema(vector_size, value_type=value_type)
        self.deleted = []
        self.added = []
        self.events = []
        self.search_query = None
        self.scalar_indices = []
        self.vector_indices = []
        self.optimized = []
        self.indices = []

    def add(self, rows):
        self.events.append("add")
        self.added.extend(rows)

    def delete(self, expression):
        self.events.append("delete")
        self.deleted.append(expression)

    def search(self, _vector):
        self.search_query = _FakeSearchQuery(self.rows)
        return self.search_query

    def create_scalar_index(self, column, **kwargs):
        self.events.append(f"scalar:{column}")
        self.scalar_indices.append((column, kwargs))
        self.indices.append(SimpleNamespace(name=f"{column}_idx"))

    def create_index(self, *args, **kwargs):
        self.vector_indices.append((args, kwargs))
        self.indices.append(SimpleNamespace(name="vector_idx"))

    def list_indices(self):
        return list(self.indices)

    def optimize(self, **kwargs):
        self.optimized.append(kwargs)
        return {"compacted": True}

    def count_rows(self, filter=None):
        assert filter is None
        return len(self.rows) + len(self.added)


class _FakeDb:
    def __init__(self, table=None):
        self.table = table
        self.created = []

    def table_names(self):
        return [self.table_name] if getattr(self, "table_name", None) else []

    def open_table(self, table_name):
        if self.table is None or table_name != getattr(self, "table_name", None):
            raise FileNotFoundError(table_name)
        return self.table

    def create_table(self, table_name, data):
        self.table_name = table_name
        self.created.append((table_name, data))
        self.table = _FakeTable()
        return self.table


def test_ensure_table_creates_missing_table(monkeypatch):
    db = _FakeDb()
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    store.ensure_table(vector_size=3)

    assert db.created
    assert db.table is not None


def test_ensure_table_rejects_vector_size_mismatch(monkeypatch):
    db = _FakeDb(table=_FakeTable(vector_size=4))
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    with pytest.raises(ValueError):
        store.ensure_table(vector_size=3)


def test_ensure_table_rejects_non_float16_vector_type(monkeypatch):
    db = _FakeDb(table=_FakeTable(vector_size=3, value_type="float32"))
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    with pytest.raises(ValueError):
        store.ensure_table(vector_size=3)


def test_ensure_table_accepts_halffloat_vector_type(monkeypatch):
    db = _FakeDb(table=_FakeTable(vector_size=3, value_type="halffloat"))
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    store.ensure_table(vector_size=3)


def test_search_applies_offset_and_filters(monkeypatch):
    rows = [
        {"thumbnail_id": 1, "media_id": 11, "movie_id": 101, "offset_seconds": 10, "_distance": 0.1},
        {"thumbnail_id": 2, "media_id": 12, "movie_id": 102, "offset_seconds": 20, "_distance": 0.2},
        {"thumbnail_id": 3, "media_id": 13, "movie_id": 103, "offset_seconds": 30, "_distance": 0.3},
    ]
    table = _FakeTable(rows=rows)
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    hits = store.search(
        query_vector=[0.1, 0.2, 0.3],
        limit=2,
        offset=1,
        movie_ids=[101, 102],
        exclude_movie_ids=[999],
    )

    assert [hit.thumbnail_id for hit in hits] == [2, 3]
    assert table.search_query.where_expr == ("movie_id IN (101, 102) AND movie_id NOT IN (999)", True)
    assert table.search_query.limit_value == 3


def test_search_reopens_table_to_see_external_updates(monkeypatch):
    old_table = _FakeTable(
        rows=[
            {"thumbnail_id": 1, "media_id": 11, "movie_id": 101, "offset_seconds": 10, "_distance": 0.1},
        ]
    )
    new_table = _FakeTable(
        rows=[
            {"thumbnail_id": 2, "media_id": 12, "movie_id": 102, "offset_seconds": 20, "_distance": 0.2},
        ]
    )
    db = _FakeDb(table=old_table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    first_hits = store.search(query_vector=[0.1, 0.2, 0.3], limit=1)

    db.table = new_table

    second_hits = store.search(query_vector=[0.1, 0.2, 0.3], limit=1)

    assert [hit.thumbnail_id for hit in first_hits] == [1]
    assert [hit.thumbnail_id for hit in second_hits] == [2]


def test_upsert_and_delete_use_expected_expressions(monkeypatch):
    table = _FakeTable(rows=[{"thumbnail_id": 9}])
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))
    monkeypatch.setattr(store, "ensure_table", lambda vector_size: None)

    store.upsert_records(
        [
            ThumbnailVectorRecord(
                thumbnail_id=1,
                media_id=10,
                movie_id=100,
                offset_seconds=12,
                vector=[0.1, 0.2],
            )
        ]
    )
    store.delete_by_media_id(10)

    assert table.deleted[0] == "thumbnail_id IN (1)"
    assert table.deleted[1] == "media_id = 10"
    assert table.added[0]["thumbnail_id"] == 1
    assert isinstance(table.added[0]["vector"][0], np.float16)


def test_ensure_scalar_indices_skips_empty_tables(monkeypatch):
    table = _FakeTable()
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    store.ensure_scalar_indices()

    assert table.scalar_indices == []


def test_ensure_scalar_indices_creates_expected_btree_indexes(monkeypatch):
    table = _FakeTable(rows=[{"thumbnail_id": 1}])
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    store.ensure_scalar_indices()

    assert [column for column, _kwargs in table.scalar_indices] == [
        "movie_id",
    ]


def test_upsert_records_first_write_adds_before_scalar_index(monkeypatch):
    table = _FakeTable()
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))
    monkeypatch.setattr(store, "ensure_table", lambda vector_size: None)

    store.upsert_records(
        [
            ThumbnailVectorRecord(
                thumbnail_id=1,
                media_id=10,
                movie_id=100,
                offset_seconds=12,
                vector=[0.1, 0.2],
            )
        ]
    )

    assert table.deleted == []
    assert table.events == ["add"]


def test_ensure_vector_index_uses_ivf_rq_when_supported(monkeypatch):
    table = _FakeTable(rows=[{"thumbnail_id": 1}])
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))
    monkeypatch.setattr(
        "src.service.discovery.lancedb_thumbnail_store._build_ivf_rq_config",
        lambda distance_type, num_partitions, num_bits, max_iterations, sample_rate: {
            "kind": "ivf_rq",
            "distance_type": distance_type,
            "num_partitions": num_partitions,
            "num_bits": num_bits,
            "max_iterations": max_iterations,
            "sample_rate": sample_rate,
        },
    )

    store.ensure_vector_index()

    assert table.vector_indices == [
        (
            ("vector",),
            {
                "config": {
                    "kind": "ivf_rq",
                    "distance_type": "cosine",
                    "num_partitions": 512,
                    "num_bits": 1,
                    "max_iterations": 50,
                    "sample_rate": 256,
                },
                "name": "vector_idx",
            },
        )
    ]


def test_ensure_vector_index_falls_back_to_ivf_pq(monkeypatch):
    table = _FakeTable(rows=[{"thumbnail_id": 1}])
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))
    monkeypatch.setattr(
        "src.service.discovery.lancedb_thumbnail_store._build_ivf_rq_config",
        lambda *args, **kwargs: None,
    )

    store.ensure_vector_index()

    assert table.vector_indices == [
        (
            (),
            {
                "metric": "cosine",
                "num_partitions": 512,
                "num_sub_vectors": 96,
                "vector_column_name": "vector",
                "index_type": "IVF_PQ",
                "name": "vector_idx",
            },
        )
    ]


def test_optimize_runs_table_optimize(monkeypatch):
    table = _FakeTable(rows=[{"thumbnail_id": 1}])
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))
    monkeypatch.setattr(store, "ensure_scalar_indices", lambda: None)
    monkeypatch.setattr(store, "ensure_vector_index", lambda: None)

    result = store.optimize()

    assert result == {"compacted": True}
    assert table.optimized == [{"cleanup_older_than": timedelta(days=0), "delete_unverified": False}]


def test_optimize_returns_false_for_empty_table(monkeypatch):
    table = _FakeTable()
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    result = store.optimize()

    assert result == {"compacted": False}
    assert table.optimized == []


def test_delete_by_thumbnail_ids_skips_empty_table(monkeypatch):
    table = _FakeTable()
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    store.delete_by_thumbnail_ids([1])

    assert table.deleted == []


def test_delete_by_media_id_skips_empty_table(monkeypatch):
    table = _FakeTable()
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    store.delete_by_media_id(10)

    assert table.deleted == []


@pytest.mark.parametrize(
    ("distance", "expected_score"),
    [
        (0.0, 1.0),
        (1.0, 0.5),
        (2.0, 0.0),
    ],
)
def test_parse_hit_maps_cosine_distance_to_zero_one_score(distance, expected_score):
    hit = LanceDbThumbnailStore._parse_hit(
        {
            "thumbnail_id": 1,
            "media_id": 2,
            "movie_id": 3,
            "offset_seconds": 4,
            "_distance": distance,
        }
    )

    assert hit.score == expected_score


def test_inspect_status_returns_table_details(monkeypatch):
    table = _FakeTable(rows=[{"thumbnail_id": 1}], vector_size=3, value_type="halffloat")
    table.indices.append(SimpleNamespace(name="vector_idx"))
    db = _FakeDb(table=table)
    db.table_name = "thumbs"
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    status = store.inspect_status()

    assert status == {
        "healthy": True,
        "uri": "/tmp/lancedb",
        "table_name": "thumbs",
        "table_exists": True,
        "row_count": 1,
        "vector_size": 3,
        "vector_dtype": "halffloat",
        "has_vector_index": True,
        "error": None,
    }


def test_inspect_status_handles_missing_table_without_error(monkeypatch):
    db = _FakeDb(table=None)
    store = LanceDbThumbnailStore(uri="/tmp/lancedb", table_name="thumbs", db=db)
    monkeypatch.setattr(LanceDbThumbnailStore, "_ensure_dependency", staticmethod(lambda: None))

    status = store.inspect_status()

    assert status == {
        "healthy": True,
        "uri": "/tmp/lancedb",
        "table_name": "thumbs",
        "table_exists": False,
        "row_count": None,
        "vector_size": None,
        "vector_dtype": None,
        "has_vector_index": None,
        "error": None,
    }
