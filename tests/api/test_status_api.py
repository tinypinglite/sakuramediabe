from src.lib.cloud115 import Cloud115CookieStatus
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import Actor, Media, MediaLibrary, Movie
from src.service.discovery.joytag_embedder_client import JoyTagInferenceUnavailableError
from src.service.playback.cloud115_backend_service import Cloud115KeepaliveService
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


def test_cloud115_cookies_status_endpoint_requires_authentication(client):
    response = client.get("/status/media-libraries/cloud115")

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
        "thumbnails": {
            "pending_media": 0,
            "retry_wait_media": 0,
            "terminal_failed_media": 0,
            "total": 0,
        },
    }


def test_cloud115_cookies_status_endpoint_returns_empty_summary(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/status/media-libraries/cloud115",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {
        "total": 0,
        "alive": 0,
        "expired": 0,
        "unavailable": 0,
    }
    assert body["libraries"] == []
    assert body["checked_at"]


def test_cloud115_cookies_status_endpoint_returns_all_libraries_and_isolates_failures(
    client,
    account_user,
    monkeypatch,
):
    MediaLibrary.create(
        name="local",
        backend="local",
        backend_config={"root_path": "/library/local"},
    )
    for index, name in enumerate(("alive-lib", "expired-lib", "down-lib"), start=1):
        MediaLibrary.create(
            name=name,
            backend="cloud115",
            backend_account_key=f"cloud115:{index}",
            backend_config={
                "cookies": f"UID={index}_A1_1700000000; CID=secret; SEID=secret",
                "root_cid": f"root-{index}",
                "app": "alipaymini",
            },
        )

    async def fake_probe(_cls, library):
        if library.name == "alive-lib":
            return Cloud115CookieStatus.ALIVE
        if library.name == "expired-lib":
            return Cloud115CookieStatus.EXPIRED
        raise RuntimeError("temporary upstream failure")

    monkeypatch.setattr(
        Cloud115KeepaliveService,
        "probe_library_cookies_status",
        classmethod(fake_probe),
    )
    token = _login(client, username=account_user.username)

    response = client.get(
        "/status/media-libraries/cloud115",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {
        "total": 3,
        "alive": 1,
        "expired": 1,
        "unavailable": 1,
    }
    assert [
        (item["name"], item["cookie_status"])
        for item in body["libraries"]
    ] == [
        ("alive-lib", "alive"),
        ("expired-lib", "expired"),
        ("down-lib", "unavailable"),
    ]
    assert all(
        set(item) == {"library_id", "name", "cookie_status"}
        for item in body["libraries"]
    )
    assert "cookies" not in response.text.lower()
    assert "secret" not in response.text


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

    library_main = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": "/library/main"})
    library_archive = MediaLibrary.create(name="Archive", backend="local", backend_config={"root_path": "/library/archive"})

    Media.create(
        movie=movie_a,
        path="/library/main/abc-001-main.mp4",
        library=library_main,
        valid=True,
        content_fingerprint="fingerprint-a-main",
        file_size_bytes=100,
    )
    # 未完成指纹计算的媒体还不能生成缩略图，不计入 pending_media。
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
        content_fingerprint="fingerprint-b",
        file_size_bytes=300,
    )
    Media.create(
        movie=movie_c,
        path="/library/archive/abc-003.mp4",
        library=library_archive,
        valid=True,
        content_fingerprint="fingerprint-c",
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
            # 仅两条有效且已有指纹、尚无 MediaThumbnail 的媒体可直接处理。
            "pending_media": 2,
            "retry_wait_media": 0,
            "terminal_failed_media": 0,
            "total": 0,
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
        "src.service.system.status_service.get_joytag_embedder_client",
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
    assert payload["joytag"]["healthy"] is True
    assert payload["joytag"]["used_device"] == "cpu"
    assert payload["joytag"]["backend"] == "cpu"
    assert payload["joytag"]["error"] is None
    assert payload["image_search_vector_store"]["healthy"] is True
    assert payload["image_search_vector_store"]["exists"] is True


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
        "src.service.system.status_service.get_joytag_embedder_client",
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
