import pytest
from qdrant_client import models
from qdrant_client.http.exceptions import ResponseHandlingException

from src.service.discovery.qdrant_plot_image_store import QdrantPlotImageStore
from src.service.discovery.qdrant_thumbnail_store import (
    QdrantThumbnailStore,
    ThumbnailVectorRecord,
)


class _ClearClient:
    def __init__(self) -> None:
        self.deleted = []
        self.closed = False

    def collection_exists(self, _collection_name: str) -> bool:
        return True

    def delete_collection(self, *, collection_name: str, timeout: int) -> None:
        self.deleted.append((collection_name, timeout))

    def close(self) -> None:
        self.closed = True


class _RetryClient:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.calls = 0
        self.closed = False

    def upsert(self, **_kwargs) -> None:
        self.calls += 1
        if self.fail:
            raise ResponseHandlingException(RuntimeError("connection reset"))

    def close(self) -> None:
        self.closed = True


class _PayloadIndexClient:
    def __init__(self) -> None:
        self.calls = []

    def collection_exists(self, _collection_name: str) -> bool:
        return True

    def create_payload_index(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _record() -> ThumbnailVectorRecord:
    return ThumbnailVectorRecord(
        thumbnail_id=1,
        media_id=2,
        movie_id=3,
        offset_seconds=4,
        vector=[0.1, 0.2],
    )


def test_clear_uses_extended_timeout_for_real_qdrant_client(monkeypatch):
    store = QdrantThumbnailStore()
    client = _ClearClient()
    requested_timeouts = []

    def _create_client(timeout_seconds: int):
        requested_timeouts.append(timeout_seconds)
        return client

    monkeypatch.setattr(store, "_create_client", _create_client)

    store.clear()

    assert requested_timeouts == [store.CLEAR_TIMEOUT_SECONDS]
    assert client.deleted == [(store.collection_name, store.CLEAR_TIMEOUT_SECONDS)]
    assert client.closed is True


def test_upsert_retries_response_handling_failure_with_fresh_client(monkeypatch):
    store = QdrantThumbnailStore()
    first_client = _RetryClient(fail=True)
    second_client = _RetryClient(fail=False)
    clients = iter((first_client, second_client))
    sleeps = []
    monkeypatch.setattr(store, "_create_client", lambda _timeout: next(clients))
    monkeypatch.setattr(
        "src.service.discovery.qdrant_thumbnail_store.time.sleep", sleeps.append
    )

    store.upsert_records([_record()])

    assert first_client.calls == 1
    assert first_client.closed is True
    assert second_client.calls == 1
    assert sleeps == [3]


def test_upsert_reraises_after_response_handling_retries_are_exhausted(monkeypatch):
    store = QdrantThumbnailStore()
    clients = [
        _RetryClient(fail=True)
        for _ in range(len(store.UPSERT_RETRY_DELAYS_SECONDS) + 1)
    ]
    client_iterator = iter(clients)
    sleeps = []
    monkeypatch.setattr(store, "_create_client", lambda _timeout: next(client_iterator))
    monkeypatch.setattr(
        "src.service.discovery.qdrant_thumbnail_store.time.sleep", sleeps.append
    )

    with pytest.raises(ResponseHandlingException):
        store.upsert_records([_record()])

    assert [client.calls for client in clients] == [1, 1, 1, 1, 1, 1]
    assert all(client.closed for client in clients)
    assert sleeps == [3, 10, 20, 60, 60]


@pytest.mark.parametrize(
    ("store_type", "expected_fields"),
    [
        (QdrantThumbnailStore, ("movie_id", "media_id")),
        (QdrantPlotImageStore, ("movie_id",)),
    ],
)
def test_scalar_indices_use_on_disk_exact_match_integer_schema(store_type, expected_fields):
    client = _PayloadIndexClient()
    store = store_type(client=client)

    store.ensure_scalar_indices()

    assert [call["field_name"] for call in client.calls] == list(expected_fields)
    assert all(call["collection_name"] == store.collection_name for call in client.calls)
    assert all(call["wait"] is True for call in client.calls)
    assert [
        call["field_schema"].model_dump(mode="json", exclude_none=True)
        for call in client.calls
    ] == [
        {
            "type": models.IntegerIndexType.INTEGER.value,
            "lookup": True,
            "range": False,
            "on_disk": True,
        }
        for _ in expected_fields
    ]
