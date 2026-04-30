from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import Actor, Media, MediaLibrary, Movie
from src.service.discovery.joytag_embedder_client import JoyTagInferenceUnavailableError
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


def test_status_endpoint_requires_authentication(client):
    response = client.get("/status")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_image_search_status_endpoint_requires_authentication(client):
    response = client.get("/status/image-search")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_metadata_provider_test_endpoint_requires_authentication(client):
    response = client.get("/status/metadata-providers/javdb/test")

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

    library_main = MediaLibrary.create(name="Main", root_path="/library/main")
    library_archive = MediaLibrary.create(name="Archive", root_path="/library/archive")

    Media.create(
        movie=movie_a,
        path="/library/main/abc-001-main.mp4",
        library=library_main,
        valid=True,
        file_size_bytes=100,
    )
    Media.create(
        movie=movie_a,
        path="/library/main/abc-001-backup.mp4",
        library=library_main,
        valid=True,
        file_size_bytes=200,
    )
    Media.create(
        movie=movie_b,
        path="/library/main/abc-002.mp4",
        library=library_main,
        valid=False,
        file_size_bytes=300,
    )
    Media.create(
        movie=movie_c,
        path="/library/archive/abc-003.mp4",
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
    }


def test_image_search_status_endpoint_returns_healthy_payload(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    class _FakeClient:
        def get_runtime_status(self):
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

    class _FakeStore:
        uri = "/data/indexes/image-search"
        table_name = "media_thumbnail_vectors"

        def inspect_status(self):
            return {
                "healthy": True,
                "uri": self.uri,
                "table_name": self.table_name,
                "table_exists": True,
                "row_count": 12,
                "vector_size": 768,
                "vector_dtype": "halffloat",
                "has_vector_index": True,
                "error": None,
            }

    monkeypatch.setattr(
        "src.service.system.status_service.get_joytag_embedder_client",
        lambda: _FakeClient(),
    )
    monkeypatch.setattr(
        "src.service.system.status_service.get_lancedb_thumbnail_store",
        lambda: _FakeStore(),
    )

    response = client.get("/status/image-search", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["healthy"] is True
    assert payload["joytag"]["healthy"] is True
    assert payload["joytag"]["used_device"] == "cpu"
    assert payload["joytag"]["backend"] == "cpu"
    assert payload["joytag"]["error"] is None
    assert payload["lancedb"]["healthy"] is True
    assert payload["lancedb"]["table_exists"] is True


def test_image_search_status_endpoint_returns_failure_payload_when_joytag_probe_fails(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, username=account_user.username)

    class _FakeClient:
        def get_runtime_status(self):
            raise JoyTagInferenceUnavailableError("probe failed")

    class _FakeStore:
        uri = "/data/indexes/image-search"
        table_name = "media_thumbnail_vectors"

        def inspect_status(self):
            return {
                "healthy": True,
                "uri": self.uri,
                "table_name": self.table_name,
                "table_exists": False,
                "row_count": None,
                "vector_size": None,
                "vector_dtype": None,
                "has_vector_index": None,
                "error": None,
            }

    monkeypatch.setattr(
        "src.service.system.status_service.get_joytag_embedder_client",
        lambda: _FakeClient(),
    )
    monkeypatch.setattr(
        "src.service.system.status_service.get_lancedb_thumbnail_store",
        lambda: _FakeStore(),
    )

    response = client.get("/status/image-search", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["healthy"] is False
    assert payload["joytag"]["healthy"] is False
    assert payload["joytag"]["error"] == "probe failed"


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

    def _build_javdb_provider(*, use_metadata_proxy: bool = False):
        assert use_metadata_proxy is False
        return _FakeJavdbProvider()

    monkeypatch.setattr(
        "src.service.system.status_service.build_javdb_provider",
        _build_javdb_provider,
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


def test_metadata_provider_test_endpoint_returns_dmm_success_payload(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, username=account_user.username)
    requested_movie_numbers = []
    description = "这是 DMM 简介" * 20

    class _FakeDmmProvider:
        def get_movie_desc(self, movie_number: str):
            requested_movie_numbers.append(movie_number)
            return description

    monkeypatch.setattr(
        "src.service.system.status_service.build_dmm_provider",
        lambda: _FakeDmmProvider(),
    )

    response = client.get(
        "/status/metadata-providers/dmm/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert requested_movie_numbers == [StatusService.METADATA_PROVIDER_TEST_MOVIE_NUMBER]
    assert payload["healthy"] is True
    assert payload["provider"] == "dmm"
    assert payload["movie_number"] == "SSNI-888"
    assert payload["description_length"] == len(description)
    assert payload["description_excerpt"] == description[:120]
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
        lambda use_metadata_proxy=False: _FailingProvider(),
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
        def get_movie_desc(self, movie_number: str):
            raise MetadataNotFoundError("movie", movie_number)

    monkeypatch.setattr(
        "src.service.system.status_service.build_dmm_provider",
        lambda: _FailingProvider(),
    )

    response = client.get(
        "/status/metadata-providers/dmm/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["healthy"] is False
    assert payload["provider"] == "dmm"
    assert payload["error"]["type"] == "metadata_not_found"
    assert payload["error"]["resource"] == "movie"
    assert payload["error"]["lookup_value"] == "SSNI-888"


def test_metadata_provider_test_endpoint_rejects_invalid_provider(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/status/metadata-providers/missav/test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_metadata_provider"
