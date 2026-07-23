from __future__ import annotations

from typing import Any

import httpx

from src.lib.cloud115.transport import Cloud115Transport


class Cloud115Capability:
    def __init__(self, transport: Cloud115Transport) -> None:
        self._transport = transport

    @property
    def _user_id(self) -> str:
        return self._transport.session.user_id

    @property
    def _user_agent(self) -> str:
        return self._transport.session.user_agent

    @property
    def _cookies_dict(self) -> dict[str, str]:
        return self._transport.session.cookies_dict

    @property
    def _client(self) -> httpx.AsyncClient:
        return self._transport._client

    def _base_headers(self) -> dict[str, str]:
        return self._transport._base_headers()

    async def _request(self, *args, **kwargs) -> httpx.Response:
        return await self._transport._request(*args, **kwargs)

    async def _request_json(self, *args, **kwargs) -> dict[str, Any]:
        return await self._transport._request_json(*args, **kwargs)

    async def _get_text(self, *args, **kwargs) -> str:
        return await self._transport._get_text(*args, **kwargs)

    @staticmethod
    def _map_errno(payload: dict[str, Any], *, endpoint: str):
        return Cloud115Transport._map_errno(payload, endpoint=endpoint)
