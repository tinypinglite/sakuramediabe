from src.model import Image, Media, MediaThumbnail, Movie
from src.service.discovery.joytag_embedder_client import JoyTagInferenceUnavailableError
from src.service.system.status_service import StatusService


class _FakeJoyTagClient:
    def __init__(self, *, should_fail: bool = False):
        self.should_fail = should_fail

    def get_runtime_status(self):
        if self.should_fail:
            raise JoyTagInferenceUnavailableError("joytag probe failed")

        class _Runtime:
            endpoint = "http://joytag-infer:8001"
            backend = "cpu"
            execution_provider = "CPUExecutionProvider"
            device = "cpu"
            device_full_name = None
            model_path = "/data/lib/joytag/model_vit_768.onnx"
            model_name = "joytag-onnxruntime"
            vector_size = 768
            image_size = 448
            available_providers = ["CPUExecutionProvider"]
            probe_latency_ms = 12

        return _Runtime()


class _FakeQdrantStore:
    url = "http://qdrant:6333"
    collection_name = "media_thumbnail_vectors"

    def __init__(self, payload: dict):
        self.payload = payload

    def inspect_status(self):
        return self.payload


def _vector_store_payload(*, healthy: bool = True, exists: bool = True, error: str | None = None):
    return {
        "healthy": healthy,
        "url": "http://qdrant:6333",
        "collection_name": "media_thumbnail_vectors",
        "exists": exists,
        "points_count": 9 if exists else None,
        "vector_size": 768 if exists else None,
        "vector_dtype": "float16" if exists else None,
        "collection_status": "green" if exists else None,
        "error": error,
    }


def _create_thumbnail(movie_number: str, *, joytag_index_status: int):
    movie = Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=movie_number,
    )
    media = Media.create(
        movie=movie,
        path=f"/library/{movie_number}.mp4",
        valid=True,
    )
    image = Image.create(
        origin=f"movies/{movie_number}/thumb.webp",
        small=f"movies/{movie_number}/thumb.webp",
        medium=f"movies/{movie_number}/thumb.webp",
        large=f"movies/{movie_number}/thumb.webp",
    )
    return MediaThumbnail.create(
        media=media,
        image=image,
        offset=10,
        joytag_index_status=joytag_index_status,
    )


def test_get_status_reads_backend_version_from_env(app, monkeypatch):
    monkeypatch.setenv(StatusService.BACKEND_VERSION_ENV_KEY, "v9.9.9")
    status = StatusService.get_status()

    assert status.backend_version == "v9.9.9"


def test_get_status_uses_default_backend_version_when_env_missing(app, monkeypatch):
    monkeypatch.delenv(StatusService.BACKEND_VERSION_ENV_KEY, raising=False)
    status = StatusService.get_status()

    assert status.backend_version == StatusService.BACKEND_VERSION_DEFAULT


def test_get_image_search_status_returns_success_and_indexing_counts(app, monkeypatch):
    _create_thumbnail("ABC-001", joytag_index_status=MediaThumbnail.JOYTAG_INDEX_STATUS_PENDING)
    _create_thumbnail("ABC-002", joytag_index_status=MediaThumbnail.JOYTAG_INDEX_STATUS_FAILED)
    _create_thumbnail("ABC-003", joytag_index_status=MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS)

    monkeypatch.setattr(
        "src.service.system.status_service.get_joytag_embedder_client",
        lambda: _FakeJoyTagClient(should_fail=False),
    )
    monkeypatch.setattr(
        "src.service.system.status_service.get_qdrant_thumbnail_store",
        lambda: _FakeQdrantStore(_vector_store_payload()),
    )

    status = StatusService.get_image_search_status()

    assert status.healthy is True
    assert status.joytag.healthy is True
    assert status.joytag.used_device == "cpu"
    assert status.joytag.backend == "cpu"
    assert status.image_search_vector_store.healthy is True
    assert status.image_search_vector_store.exists is True
    assert status.image_search_vector_store.points_count == 9
    assert status.indexing.pending_thumbnails == 1
    assert status.indexing.failed_thumbnails == 1
    assert status.indexing.success_thumbnails == 1


def test_get_image_search_status_marks_unhealthy_when_joytag_probe_fails(app, monkeypatch):
    monkeypatch.setattr(
        "src.service.system.status_service.get_joytag_embedder_client",
        lambda: _FakeJoyTagClient(should_fail=True),
    )
    monkeypatch.setattr(
        "src.service.system.status_service.get_qdrant_thumbnail_store",
        lambda: _FakeQdrantStore(_vector_store_payload(exists=False)),
    )

    status = StatusService.get_image_search_status()

    assert status.healthy is False
    assert status.joytag.healthy is False
    assert status.joytag.error == "joytag probe failed"
    assert status.image_search_vector_store.healthy is True


def test_get_image_search_status_marks_unhealthy_when_qdrant_is_unhealthy(app, monkeypatch):
    monkeypatch.setattr(
        "src.service.system.status_service.get_joytag_embedder_client",
        lambda: _FakeJoyTagClient(should_fail=False),
    )
    monkeypatch.setattr(
        "src.service.system.status_service.get_qdrant_thumbnail_store",
        lambda: _FakeQdrantStore(
            _vector_store_payload(healthy=False, exists=True, error="qdrant unavailable")
        ),
    )

    status = StatusService.get_image_search_status()

    assert status.healthy is False
    assert status.joytag.healthy is True
    assert status.image_search_vector_store.healthy is False
    assert status.image_search_vector_store.error == "qdrant unavailable"


def test_get_image_search_status_keeps_qdrant_healthy_when_collection_does_not_exist(app, monkeypatch):
    monkeypatch.setattr(
        "src.service.system.status_service.get_joytag_embedder_client",
        lambda: _FakeJoyTagClient(should_fail=False),
    )
    monkeypatch.setattr(
        "src.service.system.status_service.get_qdrant_thumbnail_store",
        lambda: _FakeQdrantStore(_vector_store_payload(exists=False)),
    )

    status = StatusService.get_image_search_status()

    assert status.healthy is True
    assert status.image_search_vector_store.healthy is True
    assert status.image_search_vector_store.exists is False
    assert status.image_search_vector_store.error is None
