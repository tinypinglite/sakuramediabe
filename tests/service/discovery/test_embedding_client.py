import httpx

from src.service.discovery.embedding_client import EmbeddingClient


def test_embedding_client_uses_generic_image_text_contract():
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/embedding-space":
            return httpx.Response(
                200,
                json={
                    "space_id": "siglip2-base-v1",
                    "dimension": 3,
                    "modalities": ["image", "text"],
                },
            )
        return httpx.Response(200, json={"dimension": 3, "vectors": [[1, 2, 3]]})

    client = EmbeddingClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(handle), base_url="http://embedding.test"
        )
    )

    assert client.describe().space_id == "siglip2-base-v1"
    assert client.embed_images([b"image"]) == [[1.0, 2.0, 3.0]]
    assert client.embed_texts(["a room"]) == [[1.0, 2.0, 3.0]]
    assert [item.url.path for item in requests] == [
        "/v1/embedding-space",
        "/v1/embed/images",
        "/v1/embed/texts",
    ]
