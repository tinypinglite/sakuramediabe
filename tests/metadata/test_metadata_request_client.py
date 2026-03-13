from typing import Any, Dict, List

import pytest

import src.metadata.provider as provider_module
from src.metadata.provider import MetadataRequestClient


class FakeHttpClient:
    def __init__(self, **kwargs: Dict[str, Any]):
        self.kwargs = kwargs

    def request(self, *args, **kwargs):
        raise AssertionError("request should not be called in proxy initialization test")


def test_metadata_request_client_initializes_httpx_client_with_optional_proxy(
    monkeypatch: pytest.MonkeyPatch,
):
    created_kwargs: List[Dict[str, Any]] = []

    def fake_client(**kwargs):
        created_kwargs.append(kwargs)
        return FakeHttpClient(**kwargs)

    monkeypatch.setattr(provider_module.httpx, "Client", fake_client)

    MetadataRequestClient()
    MetadataRequestClient(proxy="http://127.0.0.1:7890")

    assert created_kwargs[0] == {
        "timeout": MetadataRequestClient.DEFAULT_TIMEOUT,
        "trust_env": False,
    }
    assert created_kwargs[1] == {
        "proxy": "http://127.0.0.1:7890",
        "timeout": MetadataRequestClient.DEFAULT_TIMEOUT,
        "trust_env": False,
    }
