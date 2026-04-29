from __future__ import annotations

from typing import Any

import httpx

from src.config.config import settings


class MovieDescTranslationClientError(RuntimeError):
    def __init__(self, status_code: int, error_code: str, message: str):
        super().__init__(message)
        self.status_code = int(status_code)
        self.error_code = error_code
        self.message = message

    @property
    def is_empty_result(self) -> bool:
        return self.error_code == "movie_desc_translation_empty_result"

    @property
    def should_retry_then_abort_task(self) -> bool:
        # 仅把明显的瞬时异常升级为任务级中断，避免单条脏数据卡死整批队列。
        if self.is_empty_result:
            return False
        if self.error_code in {
            "movie_desc_translation_invalid_response",
            "movie_desc_translation_unavailable",
        }:
            return True
        return self.status_code == 429 or self.status_code >= 500


class MovieDescTranslationClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        connect_timeout_seconds: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        config = settings.movie_info_translation
        self.base_url = (base_url or config.base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else config.api_key
        self.model = (model if model is not None else config.model).strip()
        self.timeout_seconds = float(timeout_seconds if timeout_seconds is not None else config.timeout_seconds)
        self.connect_timeout_seconds = float(
            connect_timeout_seconds
            if connect_timeout_seconds is not None
            else config.connect_timeout_seconds
        )
        self._http_client = http_client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        normalized_api_key = (self.api_key or "").strip()
        if normalized_api_key:
            headers["Authorization"] = f"Bearer {normalized_api_key}"
        return headers

    def _build_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(timeout=self.timeout_seconds, connect=self.connect_timeout_seconds)

    def _client(self) -> httpx.Client:
        return self._http_client or httpx.Client(
            base_url=self.base_url,
            timeout=self._build_timeout(),
            headers=self._headers(),
            trust_env=False,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = self._client()
        close_after_request = self._http_client is None
        try:
            response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise MovieDescTranslationClientError(
                503,
                "movie_desc_translation_unavailable",
                "影片简介翻译服务请求超时",
            ) from exc
        except httpx.NetworkError as exc:
            raise MovieDescTranslationClientError(
                503,
                "movie_desc_translation_unavailable",
                "影片简介翻译服务不可达",
            ) from exc
        except httpx.HTTPError as exc:
            raise MovieDescTranslationClientError(
                502,
                "movie_desc_translation_failed",
                f"影片简介翻译服务请求失败: {exc}",
            ) from exc
        finally:
            if close_after_request:
                client.close()
        return response

    @staticmethod
    def _try_parse_json_response(response: httpx.Response) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _parse_json_response(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MovieDescTranslationClientError(
                response.status_code,
                "movie_desc_translation_invalid_response",
                "影片简介翻译服务返回了非法 JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise MovieDescTranslationClientError(
                response.status_code,
                "movie_desc_translation_invalid_response",
                "影片简介翻译服务返回了非法响应结构",
            )
        return payload

    @classmethod
    def _raise_from_response(cls, response: httpx.Response) -> None:
        payload = cls._try_parse_json_response(response)
        if payload is not None:
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or response.text or "请求失败")
                error_code = str(error.get("code") or "movie_desc_translation_failed")
            else:
                message = str(payload.get("detail") or response.text or "请求失败")
                error_code = "movie_desc_translation_failed"
            raise MovieDescTranslationClientError(response.status_code, error_code, message)

        # 错误页不是 JSON 时也要保留真实状态码，避免把 429/5xx 误判成统一 502。
        message = str(response.text or "请求失败").strip() or f"请求失败 status_code={response.status_code}"
        error_code = "movie_desc_translation_failed"
        raise MovieDescTranslationClientError(response.status_code, error_code, message)

    def translate(self, *, system_prompt: str, source_text: str) -> str:
        response = self._request(
            "POST",
            "/v1/chat/completions",
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": source_text},
                ],
            },
        )
        if response.status_code >= 400:
            self._raise_from_response(response)

        payload = self._parse_json_response(response)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise MovieDescTranslationClientError(
                response.status_code,
                "movie_desc_translation_invalid_response",
                "影片简介翻译服务未返回候选结果",
            )
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise MovieDescTranslationClientError(
                response.status_code,
                "movie_desc_translation_invalid_response",
                "影片简介翻译服务返回了非法候选结果",
            )
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise MovieDescTranslationClientError(
                response.status_code,
                "movie_desc_translation_invalid_response",
                "影片简介翻译服务返回了非法消息结构",
            )
        translated_text = str(message.get("content") or "").strip()
        if not translated_text:
            raise MovieDescTranslationClientError(
                response.status_code,
                "movie_desc_translation_empty_result",
                "影片简介翻译服务返回了空译文",
            )
        return translated_text
