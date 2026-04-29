import httpx
import pytest

from src.service.discovery.joytag_embedder_client import (
    JoyTagEmbedderClient,
    JoyTagEmbeddingItemError,
    JoyTagEmbeddingResult,
    JoyTagInferenceUnavailableError,
)


def test_infer_image_bytes_parses_successful_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/embeddings/images"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "index": 0,
                        "ok": True,
                        "vector": [0.1, 0.2, 0.3],
                        "metadata": {"provider": "joytag"},
                    }
                ]
            },
        )

    client = JoyTagEmbedderClient(
        base_url="http://joytag-infer:8001",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://joytag-infer:8001"),
    )

    result = client.infer_image_bytes(b"image")

    assert isinstance(result, JoyTagEmbeddingResult)
    assert result.vector == [0.1, 0.2, 0.3]
    assert result.metadata["provider"] == "joytag"


def test_infer_image_batch_returns_item_error_without_failing_whole_batch():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "index": 0,
                        "ok": True,
                        "vector": [0.1, 0.2, 0.3],
                        "metadata": {"provider": "joytag"},
                    },
                    {
                        "index": 1,
                        "ok": False,
                        "error_code": "invalid_image",
                        "error_message": "Invalid image bytes",
                    },
                ]
            },
        )

    client = JoyTagEmbedderClient(
        base_url="http://joytag-infer:8001",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://joytag-infer:8001"),
    )

    results = client.infer_image_batch([b"image-1", b"image-2"])

    assert isinstance(results[0], JoyTagEmbeddingResult)
    assert isinstance(results[1], JoyTagEmbeddingItemError)
    assert results[1].error_code == "invalid_image"


def test_get_runtime_status_maps_network_error_to_unavailable():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = JoyTagEmbedderClient(
        base_url="http://joytag-infer:8001",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://joytag-infer:8001"),
    )

    with pytest.raises(JoyTagInferenceUnavailableError):
        client.get_runtime_status()
