from src.service.discovery.qdrant_thumbnail_store import QdrantThumbnailStore


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
