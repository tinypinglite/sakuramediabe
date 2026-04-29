import json
import httpx
import pytest

from src.service.catalog.movie_desc_translation_client import (
    MovieDescTranslationClient,
    MovieDescTranslationClientError,
)


def test_translate_posts_chat_completions_request():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer secret-token"
        payload = json.loads(request.read().decode("utf-8"))
        assert payload["model"] == "translator-v1"
        assert payload["temperature"] == 0
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "中文简介",
                        }
                    }
                ]
            },
        )

    client = MovieDescTranslationClient(
        base_url="http://llm.internal:9000",
        api_key="secret-token",
        model="translator-v1",
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://llm.internal:9000",
            headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
        ),
    )

    translated_text = client.translate(
        system_prompt="translate this text",
        source_text="これはテストです",
    )

    assert translated_text == "中文简介"


def test_translate_accepts_missing_api_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "中文简介"}}]},
        )

    client = MovieDescTranslationClient(
        base_url="http://llm.internal:9000",
        api_key="",
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://llm.internal:9000",
            headers={"Content-Type": "application/json"},
        ),
    )

    assert client.translate(system_prompt="sys", source_text="ja") == "中文简介"


def test_translate_raises_error_when_response_is_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "   "}}]},
        )

    client = MovieDescTranslationClient(
        base_url="http://llm.internal:9000",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.internal:9000"),
    )

    with pytest.raises(MovieDescTranslationClientError) as exc_info:
        client.translate(system_prompt="sys", source_text="ja")

    assert exc_info.value.error_code == "movie_desc_translation_empty_result"


def test_translate_maps_network_error_to_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = MovieDescTranslationClient(
        base_url="http://llm.internal:9000",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.internal:9000"),
    )

    with pytest.raises(MovieDescTranslationClientError) as exc_info:
        client.translate(system_prompt="sys", source_text="ja")

    assert exc_info.value.error_code == "movie_desc_translation_unavailable"


def test_translate_maps_http_error_payload_to_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": "rate_limit", "message": "too many requests"}},
        )

    client = MovieDescTranslationClient(
        base_url="http://llm.internal:9000",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.internal:9000"),
    )

    with pytest.raises(MovieDescTranslationClientError) as exc_info:
        client.translate(system_prompt="sys", source_text="ja")

    assert exc_info.value.status_code == 429
    assert exc_info.value.error_code == "rate_limit"


def test_translate_preserves_status_code_for_non_json_error_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            text="<html>service unavailable</html>",
            headers={"Content-Type": "text/html"},
        )

    client = MovieDescTranslationClient(
        base_url="http://llm.internal:9000",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.internal:9000"),
    )

    with pytest.raises(MovieDescTranslationClientError) as exc_info:
        client.translate(system_prompt="sys", source_text="ja")

    assert exc_info.value.status_code == 503
    assert exc_info.value.error_code == "movie_desc_translation_failed"
    assert "service unavailable" in exc_info.value.message


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_translate_preserves_status_code_for_http_error(status_code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"detail": f"failed_{status_code}"},
        )

    client = MovieDescTranslationClient(
        base_url="http://llm.internal:9000",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.internal:9000"),
    )

    with pytest.raises(MovieDescTranslationClientError) as exc_info:
        client.translate(system_prompt="sys", source_text="ja")

    assert exc_info.value.status_code == status_code
    assert exc_info.value.message == f"failed_{status_code}"


def test_translate_raises_invalid_response_when_choices_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "chatcmpl-test"},
        )

    client = MovieDescTranslationClient(
        base_url="http://llm.internal:9000",
        http_client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.internal:9000"),
    )

    with pytest.raises(MovieDescTranslationClientError) as exc_info:
        client.translate(system_prompt="sys", source_text="ja")

    assert exc_info.value.status_code == 200
    assert exc_info.value.error_code == "movie_desc_translation_invalid_response"
