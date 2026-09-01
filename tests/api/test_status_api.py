import pytest

from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import Actor, BackgroundTaskRun, Media, MediaLibrary, Movie
from src.service.discovery.embedding_client import EmbeddingClientError
from src.service.system.status_service import StatusService


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


@pytest.mark.parametrize(
    "path",
    [
        "/status",
        "/status/image-search",
        "/status/metadata-providers/javdb/test",
    ],
)
def test_status_endpoints_require_authentication(client, path):
    response = client.get(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_status_endpoint_returns_zero_summary_when_library_is_empty(client, account_user, monkeypatch):
    monkeypatch.setenv(StatusService.BACKEND_VERSION_ENV_KEY, "v9.9.9")
    token = _login(client, username=account_user.username)

    response = client.get("/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "backend_version": "v9.9.9",
        "actors": {
            "female_total": 0,
            "female_subscribed": 0,
        },
        "movies": {
            "total": 0,
            "subscribed": 0,
            "playable": 0,
        },
        "media_files": {
            "total": 0,
            "total_size_bytes": 0,
        },
        "media_libraries": {
            "total": 0,
        },
        "thumbnails": {
            "pending_media": 0,
            "retry_wait_media": 0,
            "terminal_failed_media": 0,
            "total": 0,
        },
    }


def test_status_endpoint_returns_aggregated_summary(client, account_user, monkeypatch):
    monkeypatch.setenv(StatusService.BACKEND_VERSION_ENV_KEY, "v9.9.9")
    token = _login(client, username=account_user.username)

    Actor.create(name="actor-1", javdb_id="ActorA1", gender=1, is_subscribed=True)
    Actor.create(name="actor-2", javdb_id="ActorA2", gender=1, is_subscribed=False)
    Actor.create(name="actor-3", javdb_id="ActorA3", gender=2, is_subscribed=True)
    Actor.create(name="actor-4", javdb_id="ActorA4", gender=0, is_subscribed=True)

    movie_a = _create_movie("ABC-001", "MovieA1", is_subscribed=True)
    movie_b = _create_movie("ABC-002", "MovieA2", is_subscribed=False)
    movie_c = _create_movie("ABC-003", "MovieA3", is_subscribed=True)

    library_main = MediaLibrary.create(name="Main", provider_key="test", provider_config={})
    library_archive = MediaLibrary.create(
        name="Archive", provider_key="test", provider_config={}
    )

    Media.create(
        movie=movie_a,
        file_name="abc-001-main.mp4",
        library=library_main,
        valid=True,
        file_size_bytes=100,
    )
    Media.create(
        movie=movie_a,
        file_name="abc-001-backup.mp4",
        library=library_main,
        valid=True,
        file_size_bytes=200,
    )
    Media.create(
        movie=movie_b,
        file_name="abc-002.mp4",
        library=library_main,
        valid=False,
        file_size_bytes=300,
    )
    Media.create(
        movie=movie_c,
        file_name="abc-003.mp4",
        library=library_archive,
        valid=True,
        file_size_bytes=400,
    )

    response = client.get("/status", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "backend_version": "v9.9.9",
        "actors": {
            "female_total": 2,
            "female_subscribed": 1,
        },
        "movies": {
            "total": 3,
            "subscribed": 2,
            "playable": 2,
        },
        "media_files": {
            "total": 4,
            "total_size_bytes": 1000,
        },
        "media_libraries": {
            "total": 2,
        },
        "thumbnails": {
            # 三条有效且尚无 MediaThumbnail 的媒体可直接处理。
            "pending_media": 3,
            "retry_wait_media": 0,
            "terminal_failed_media": 0,
            "total": 0,
        },
    }


def test_image_search_status_endpoint_returns_healthy_payload(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    class _FakeClient:
        def describe(self):
            class _Runtime:
                space_id = "siglip2-base"
                dimension = 768
                modalities = {"image", "text"}

            return _Runtime()

    class _FakeStore:
        url = "http://qdrant:6333"
        collection_name = "media_thumbnail_vectors"

        def inspect_status(self):
            return {
                "healthy": True,
                "url": self.url,
                "collection_name": self.collection_name,
                "exists": True,
                "points_count": 12,
                "vector_size": 768,
                "vector_dtype": "float16",
                "collection_status": "green",
                "error": None,
            }

    monkeypatch.setattr(
        "src.service.system.status_service.get_embedding_client",
        lambda: _FakeClient(),
    )
    monkeypatch.setattr(
        "src.service.system.status_service.get_qdrant_thumbnail_store",
        lambda: _FakeStore(),
    )

    response = client.get("/status/image-search", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["healthy"] is True
    assert payload["embedding_service"]["healthy"] is True
    assert payload["embedding_service"]["space_id"] == "siglip2-base"
    assert payload["embedding_service"]["dimension"] == 768
    assert payload["embedding_service"]["error"] is None
    assert payload["image_search_vector_store"]["healthy"] is True
    assert payload["image_search_vector_store"]["exists"] is True
    assert payload["indexing"] == {
        "pending_thumbnails": 0,
        "failed_thumbnails": 0,
    }
    assert payload["index_space"] == {
        "state": "uninitialized",
        "indexed_space_id": None,
        "current_space_id": "siglip2-base",
        "is_rebuilding": False,
    }

    BackgroundTaskRun.create(
        task_key="image_search_index",
        task_name="图搜索索引",
        trigger_type="manual",
        state="pending",
        params={"reset": True},
    )

    response = client.get("/status/image-search", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["index_space"]["is_rebuilding"] is True


def test_image_search_status_endpoint_returns_failure_payload_when_embedding_probe_fails(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, username=account_user.username)

    class _FakeClient:
        def describe(self):
            raise EmbeddingClientError(503, "image_search_inference_unavailable", "probe failed")

    class _FakeStore:
        url = "http://qdrant:6333"
        collection_name = "media_thumbnail_vectors"

        def inspect_status(self):
            return {
                "healthy": True,
                "url": self.url,
                "collection_name": self.collection_name,
                "exists": False,
                "points_count": None,
                "vector_size": None,
                "vector_dtype": None,
                "collection_status": None,
                "error": None,
            }

    monkeypatch.setattr(
        "src.service.system.status_service.get_embedding_client",
        lambda: _FakeClient(),
    )
    monkeypatch.setattr(
        "src.service.system.status_service.get_qdrant_thumbnail_store",
        lambda: _FakeStore(),
    )

    response = client.get("/status/image-search", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["healthy"] is False
    assert payload["embedding_service"]["healthy"] is False
    assert payload["embedding_service"]["error"] == "probe failed"
    assert payload["index_space"]["state"] == "unavailable"


def test_image_search_reset_endpoint_queues_background_rebuild(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/image-search/reset",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202
    task_run = BackgroundTaskRun.get_by_id(response.json()["task_run_id"])
    assert task_run.task_key == "image_search_index"
    assert task_run.params == {"reset": True}


def test_metadata_provider_test_endpoint_returns_javdb_success_payload(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, username=account_user.username)
    requested_movie_numbers = []

    class _FakeJavdbProvider:
        def get_movie_by_number(self, movie_number: str):
            requested_movie_numbers.append(movie_number)

            class _Detail:
                javdb_id = "javdb-ssni-888"
                title = "SSNI-888 标题"
                actors = [object(), object()]
                tags = [object(), object(), object()]

            return _Detail()

    monkeypatch.setattr(
        "src.service.system.status_service.build_javdb_provider",
        lambda: _FakeJavdbProvider(),
    )

    response = client.get(
        "/status/metadata-providers/javdb/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert requested_movie_numbers == [StatusService.METADATA_PROVIDER_TEST_MOVIE_NUMBER]
    assert payload["healthy"] is True
    assert payload["provider"] == "javdb"
    assert payload["movie_number"] == "SSNI-888"
    assert payload["javdb_id"] == "javdb-ssni-888"
    assert payload["title"] == "SSNI-888 标题"
    assert payload["actors_count"] == 2
    assert payload["tags_count"] == 3
    assert payload["error"] is None


def test_metadata_provider_test_endpoint_returns_request_failure_payload(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, username=account_user.username)

    class _FailingProvider:
        def get_movie_by_number(self, movie_number: str):
            raise MetadataRequestError("GET", "https://javdb.example/api", "timeout")

    monkeypatch.setattr(
        "src.service.system.status_service.build_javdb_provider",
        lambda: _FailingProvider(),
    )

    response = client.get(
        "/status/metadata-providers/javdb/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["healthy"] is False
    assert payload["provider"] == "javdb"
    assert payload["error"]["type"] == "metadata_request_error"
    assert payload["error"]["method"] == "GET"
    assert payload["error"]["url"] == "https://javdb.example/api"
    assert "metadata request failed" in payload["error"]["message"]


def test_metadata_provider_test_endpoint_returns_not_found_failure_payload(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, username=account_user.username)

    class _FailingProvider:
        def get_movie_by_number(self, movie_number: str):
            raise MetadataNotFoundError("movie", movie_number)

    monkeypatch.setattr(
        "src.service.system.status_service.build_javdb_provider",
        lambda: _FailingProvider(),
    )

    response = client.get(
        "/status/metadata-providers/javdb/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["healthy"] is False
    assert payload["provider"] == "javdb"
    assert payload["error"]["type"] == "metadata_not_found"
    assert payload["error"]["resource"] == "movie"
    assert payload["error"]["lookup_value"] == "SSNI-888"


def test_metadata_provider_test_endpoint_rejects_invalid_provider(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/status/metadata-providers/unknown/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_metadata_provider"
