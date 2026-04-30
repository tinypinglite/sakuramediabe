from fastapi.testclient import TestClient

from joytag_infer.app import create_app
from joytag_infer.settings import JoyTagInferSettings


class _FakeRuntime:
    backend = "cpu"
    model_name = "joytag-onnxruntime"
    execution_provider = "CPUExecutionProvider"
    device = "cpu"
    image_size = 448
    vector_size = 768

    def runtime_info(self, *, probe: bool = True):
        assert probe is True
        return {
            "backend": self.backend,
            "execution_provider": self.execution_provider,
            "device": self.device,
            "device_full_name": None,
            "vector_size": self.vector_size,
            "image_size": self.image_size,
            "model_name": self.model_name,
            "model_path": "/data/lib/joytag/model_vit_768.onnx",
            "available_providers": ["CPUExecutionProvider"],
            "probe_latency_ms": 12,
        }

    def embed_image_batch(self, image_bytes_list: list[bytes]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in image_bytes_list]


def test_runtime_endpoint_returns_runtime_payload():
    app = create_app(
        runtime=_FakeRuntime(),
        settings=JoyTagInferSettings(api_key=None),
    )
    client = TestClient(app)

    response = client.get("/v1/runtime")

    assert response.status_code == 200
    payload = response.json()
    assert payload["backend"] == "cpu"
    assert payload["execution_provider"] == "CPUExecutionProvider"
    assert payload["device"] == "cpu"
    assert payload["vector_size"] == 768


def test_embeddings_endpoint_returns_partial_failure_items():
    app = create_app(
        runtime=_FakeRuntime(),
        settings=JoyTagInferSettings(api_key=None),
    )
    client = TestClient(app)

    response = client.post(
        "/v1/embeddings/images",
        files=[
            ("files", ("first.png", b"abc", "image/png")),
            ("files", ("empty.png", b"", "image/png")),
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["ok"] is True
    assert payload["items"][0]["vector"] == [0.1, 0.2, 0.3]
    assert payload["items"][1]["ok"] is False
    assert payload["items"][1]["error_code"] == "empty_image"


def test_embeddings_endpoint_requires_bearer_token_when_api_key_is_configured():
    app = create_app(
        runtime=_FakeRuntime(),
        settings=JoyTagInferSettings(api_key="secret-token"),
    )
    client = TestClient(app)

    unauthorized = client.get("/healthz")
    authorized = client.get("/healthz", headers={"Authorization": "Bearer secret-token"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
