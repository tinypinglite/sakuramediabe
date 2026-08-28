from functools import lru_cache
from typing import Any

import httpx
from pydantic import BaseModel

from src.config.config import settings


class EmbeddingSpace(BaseModel):
    space_id: str
    dimension: int
    modalities: set[str]


class EmbeddingClientError(RuntimeError):
    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


class EmbeddingClient:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self.base_url = settings.image_search.inference_base_url.rstrip("/")
        self.api_key = settings.image_search.inference_api_key
        self._http_client = http_client

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        client = self._http_client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                settings.image_search.inference_timeout_seconds,
                connect=settings.image_search.inference_connect_timeout_seconds,
            ),
            headers=headers,
            trust_env=False,
        )
        try:
            response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise EmbeddingClientError(
                503, "image_search_inference_unavailable", "Embedding service timed out"
            ) from exc
        except httpx.NetworkError as exc:
            raise EmbeddingClientError(
                503,
                "image_search_inference_unavailable",
                "Embedding service is unreachable",
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingClientError(
                502, "image_search_inference_failed", "Embedding service request failed"
            ) from exc
        finally:
            if self._http_client is None:
                client.close()
        if response.status_code >= 400:
            raise EmbeddingClientError(
                response.status_code,
                "image_search_inference_failed",
                "Embedding service rejected the request",
            )
        return response

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise EmbeddingClientError(
                502,
                "image_search_inference_failed",
                "Embedding service returned invalid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise EmbeddingClientError(
                502,
                "image_search_inference_failed",
                "Embedding service returned invalid payload",
            )
        return payload

    def describe(self) -> EmbeddingSpace:
        payload = self._payload(self._request("GET", "/v1/embedding-space"))
        space = EmbeddingSpace(
            space_id=str(payload.get("space_id") or ""),
            dimension=int(payload.get("dimension") or 0),
            modalities={str(value) for value in payload.get("modalities") or []},
        )
        if (
            not space.space_id
            or space.dimension <= 0
            or not {"image", "text"} <= space.modalities
        ):
            raise EmbeddingClientError(
                502,
                "image_search_inference_failed",
                "Embedding service returned invalid space",
            )
        return space

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        if not images:
            return []
        files = [
            ("files", (f"image-{index}.png", image, "application/octet-stream"))
            for index, image in enumerate(images)
        ]
        return self._vectors(
            self._payload(self._request("POST", "/v1/embed/images", files=files)),
            len(images),
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("text query must not be empty")
        return self._vectors(
            self._payload(
                self._request("POST", "/v1/embed/texts", json={"texts": texts})
            ),
            len(texts),
        )

    @staticmethod
    def _vectors(payload: dict[str, Any], expected: int) -> list[list[float]]:
        vectors = payload.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != expected:
            raise EmbeddingClientError(
                502,
                "image_search_inference_failed",
                "Embedding service returned invalid vectors",
            )
        try:
            return [[float(value) for value in vector] for vector in vectors]
        except (TypeError, ValueError) as exc:
            raise EmbeddingClientError(
                502,
                "image_search_inference_failed",
                "Embedding service returned invalid vectors",
            ) from exc


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    return EmbeddingClient()
