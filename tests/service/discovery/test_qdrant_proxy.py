from src.config.config import settings
from src.service.discovery import qdrant_movie_similarity_store, qdrant_thumbnail_store


class _CapturingQdrantClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_thumbnail_store_disables_environment_proxy(monkeypatch):
    store = qdrant_thumbnail_store.QdrantThumbnailStore(
        url="http://qdrant:6333",
        api_key="secret",
    )
    monkeypatch.setattr(
        qdrant_thumbnail_store,
        "QdrantClient",
        _CapturingQdrantClient,
    )
    monkeypatch.setattr(settings.qdrant, "enabled", True)
    monkeypatch.setattr(settings.image_search, "enabled", True)

    client = store._create_client(7)

    assert client.kwargs == {
        "url": "http://qdrant:6333",
        "api_key": "secret",
        "timeout": 7,
        "trust_env": False,
    }


def test_movie_similarity_store_disables_environment_proxy(monkeypatch):
    store = qdrant_movie_similarity_store.QdrantMovieSimilarityStore(
        url="http://qdrant:6333",
        api_key="secret",
    )
    monkeypatch.setattr(
        qdrant_movie_similarity_store,
        "QdrantClient",
        _CapturingQdrantClient,
    )

    client = store._get_client()

    assert client.kwargs == {
        "url": "http://qdrant:6333",
        "api_key": "secret",
        "timeout": store.CLIENT_TIMEOUT_SECONDS,
        "trust_env": False,
    }
