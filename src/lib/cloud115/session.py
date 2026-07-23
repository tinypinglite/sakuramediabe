from __future__ import annotations

import asyncio
import re

import httpx

from src.lib.cloud115.exceptions import Cloud115AuthError


class Cloud115Session:
    """Cloud115 账号身份、User-Agent 与动态 cookies 会话。"""

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    UID_PATTERN = re.compile(r"UID=(\d+)_")

    def __init__(self, cookies: str, *, user_agent: str | None = None) -> None:
        if not cookies or "UID=" not in cookies:
            raise Cloud115AuthError("cookies missing or has no UID field")
        self._cookies: dict[str, str] = self.parse_cookies(cookies)
        self._user_id = self.parse_user_id_from_dict(self._cookies)
        self._user_agent = user_agent or self.DEFAULT_USER_AGENT
        self.lock = asyncio.Lock()

    @staticmethod
    def parse_cookies(cookies: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in cookies.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            if key:
                result[key] = value.strip()
        return result

    @classmethod
    def parse_user_id_from_dict(cls, cookies_dict: dict[str, str]) -> str:
        uid = cookies_dict.get("UID", "")
        match = cls.UID_PATTERN.match(f"UID={uid}") if uid else None
        if not match:
            raise Cloud115AuthError(
                "UID missing or malformed (expected 'UID=<int>_A1_<ts>')"
            )
        return match.group(1)

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def user_agent(self) -> str:
        return self._user_agent

    @property
    def cookies_dict(self) -> dict[str, str]:
        return self._cookies

    def snapshot_cookies(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self._cookies.items())

    def update_cookies(self, cookies: str) -> None:
        if not cookies or "UID=" not in cookies:
            raise Cloud115AuthError("cookies missing or has no UID field")
        next_cookies = self.parse_cookies(cookies)
        next_user_id = self.parse_user_id_from_dict(next_cookies)
        self._cookies = next_cookies
        self._user_id = next_user_id

    def merge_set_cookies(self, response: httpx.Response) -> None:
        set_cookies = (
            response.headers.get_list("set-cookie")
            if hasattr(response.headers, "get_list")
            else []
        )
        for line in set_cookies:
            head = line.split(";", 1)[0].strip()
            if not head or "=" not in head:
                continue
            key, _, value = head.partition("=")
            key = key.strip()
            if not key:
                continue
            if value == "" or value == '""':
                self._cookies.pop(key, None)
            else:
                self._cookies[key] = value.strip()
