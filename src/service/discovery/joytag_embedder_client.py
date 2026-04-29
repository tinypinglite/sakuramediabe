from functools import lru_cache
from typing import Any

import httpx
from pydantic import BaseModel, Field

from src.config.config import settings


class JoyTagEmbeddingResult(BaseModel):
    vector: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)


class JoyTagEmbeddingItemError(BaseModel):
    index: int
    error_code: str
    error_message: str


class JoyTagRuntimeStatus(BaseModel):
    backend: str | None = None
    execution_provider: str | None = None
    device: str | None = None
    device_full_name: str | None = None
    vector_size: int | None = None
    image_size: int | None = None
    model_name: str | None = None
    model_path: str | None = None
    endpoint: str | None = None
    available_providers: list[str] = Field(default_factory=list)
    probe_latency_ms: int | None = None


class JoyTagInferenceClientError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.error_code = error_code
        self.message = message


class JoyTagInferenceUnavailableError(JoyTagInferenceClientError):
    def __init__(self, message: str) -> None:
        super().__init__(503, "image_search_inference_unavailable", message)


class JoyTagInferenceUpstreamError(JoyTagInferenceClientError):
    def __init__(self, message: str) -> None:
        super().__init__(502, "image_search_inference_failed", message)


class JoyTagEmbedderClient:
    model_name = "joytag-remote"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        connect_timeout_seconds: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or settings.image_search.inference_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.image_search.inference_api_key
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else settings.image_search.inference_timeout_seconds
        )
        self.connect_timeout_seconds = float(
            connect_timeout_seconds
            if connect_timeout_seconds is not None
            else settings.image_search.inference_connect_timeout_seconds
        )
        self._http_client = http_client

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            timeout=self.timeout_seconds,
            connect=self.connect_timeout_seconds,
        )

    def _client(self) -> httpx.Client:
        return self._http_client or httpx.Client(
            base_url=self.base_url,
            timeout=self._build_timeout(),
            headers=self._headers(),
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = self._client()
        close_after_request = self._http_client is None
        try:
            response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise JoyTagInferenceUnavailableError("JoyTag inference service timed out") from exc
        except httpx.NetworkError as exc:
            raise JoyTagInferenceUnavailableError("JoyTag inference service is unreachable") from exc
        except httpx.HTTPError as exc:
            raise JoyTagInferenceUpstreamError(f"JoyTag inference request failed: {exc}") from exc
        finally:
            if close_after_request:
                client.close()
        return response

    @staticmethod
    def _parse_json_response(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise JoyTagInferenceUpstreamError("JoyTag inference service returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise JoyTagInferenceUpstreamError("JoyTag inference service returned invalid payload")
        return payload

    @classmethod
    def _raise_from_response(cls, response: httpx.Response) -> None:
        payload = cls._parse_json_response(response)
        detail = payload.get("detail")
        if isinstance(detail, dict):
            error_code = str(detail.get("error_code") or "image_search_inference_failed")
            message = str(detail.get("message") or response.text or "JoyTag inference request failed")
        else:
            error_code = "image_search_inference_failed"
            message = str(detail or response.text or "JoyTag inference request failed")
        if response.status_code == 503:
            raise JoyTagInferenceUnavailableError(message)
        if response.status_code >= 500:
            raise JoyTagInferenceUpstreamError(message)
        raise JoyTagInferenceClientError(response.status_code, error_code, message)

    @staticmethod
    def _decode_vector(item: dict[str, Any]) -> list[float]:
        vector = item.get("vector")
        if isinstance(vector, list):
            return [float(value) for value in vector]
        raise JoyTagInferenceUpstreamError("JoyTag inference response is missing vector payload")

    @classmethod
    def _parse_embedding_items(
        cls,
        payload: dict[str, Any],
        *,
        expected_count: int,
    ) -> list[JoyTagEmbeddingResult | JoyTagEmbeddingItemError]:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or len(raw_items) != expected_count:
            raise JoyTagInferenceUpstreamError("JoyTag inference returned invalid batch size")
        items: list[JoyTagEmbeddingResult | JoyTagEmbeddingItemError] = []
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                raise JoyTagInferenceUpstreamError("JoyTag inference returned invalid batch item")
            item_index = int(raw_item.get("index", index))
            if item_index != index:
                raise JoyTagInferenceUpstreamError("JoyTag inference returned out-of-order batch item")
            ok = bool(raw_item.get("ok", False))
            if not ok:
                items.append(
                    JoyTagEmbeddingItemError(
                        index=item_index,
                        error_code=str(raw_item.get("error_code") or "image_search_inference_failed"),
                        error_message=str(
                            raw_item.get("error_message") or "JoyTag inference item failed"
                        ),
                    )
                )
                continue
            items.append(
                JoyTagEmbeddingResult(
                    vector=cls._decode_vector(raw_item),
                    metadata=dict(raw_item.get("metadata") or {}),
                )
            )
        return items

    def infer_image_bytes(self, image_bytes: bytes) -> JoyTagEmbeddingResult:
        if not image_bytes:
            raise ValueError("image file is empty")
        items = self.infer_image_batch([image_bytes])
        result = items[0]
        if isinstance(result, JoyTagEmbeddingItemError):
            if result.error_code in {"invalid_image", "empty_image"}:
                raise ValueError(result.error_message)
            raise JoyTagInferenceClientError(400, result.error_code, result.error_message)
        return result

    def infer_image_batch(
        self,
        image_bytes_list: list[bytes],
    ) -> list[JoyTagEmbeddingResult | JoyTagEmbeddingItemError]:
        if not image_bytes_list:
            return []
        files = []
        for index, image_bytes in enumerate(image_bytes_list):
            files.append(
                (
                    "files",
                    (f"image-{index}.png", image_bytes, "application/octet-stream"),
                )
            )
        response = self._request("POST", "/v1/embeddings/images", files=files)
        if response.status_code >= 400:
            self._raise_from_response(response)
        payload = self._parse_json_response(response)
        return self._parse_embedding_items(payload, expected_count=len(image_bytes_list))

    def get_runtime_status(self) -> JoyTagRuntimeStatus:
        response = self._request("GET", "/v1/runtime")
        if response.status_code >= 400:
            self._raise_from_response(response)
        payload = self._parse_json_response(response)
        return JoyTagRuntimeStatus(
            backend=(str(payload.get("backend")) if payload.get("backend") is not None else None),
            execution_provider=(
                str(payload.get("execution_provider"))
                if payload.get("execution_provider") is not None
                else None
            ),
            device=(str(payload.get("device")) if payload.get("device") is not None else None),
            device_full_name=(
                str(payload.get("device_full_name"))
                if payload.get("device_full_name") is not None
                else None
            ),
            vector_size=(
                int(payload.get("vector_size")) if payload.get("vector_size") is not None else None
            ),
            image_size=(
                int(payload.get("image_size")) if payload.get("image_size") is not None else None
            ),
            model_name=(
                str(payload.get("model_name")) if payload.get("model_name") is not None else None
            ),
            model_path=(
                str(payload.get("model_path")) if payload.get("model_path") is not None else None
            ),
            endpoint=self.base_url,
            available_providers=[
                str(item) for item in list(payload.get("available_providers") or [])
            ],
            probe_latency_ms=(
                int(payload.get("probe_latency_ms"))
                if payload.get("probe_latency_ms") is not None
                else None
            ),
        )


@lru_cache(maxsize=1)
def get_joytag_embedder_client() -> JoyTagEmbedderClient:
    return JoyTagEmbedderClient()
