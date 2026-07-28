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

    # 会话真正需要的 cookie：三件套身份（UID/CID/SEID）+ KID + 阿里云 WAF token。
    #
    # 必须按白名单收，不能无差别 merge 服务端下发的 Set-Cookie：WAF 会持续下发随机名
    # （32 位 hex）的一次性挑战 cookie，全盘保留会让 Cookie 请求头无上限增长，越过
    # webapi 前置 nginx 的 8KB 单条头上限后，**所有**请求都被回
    # 400 "Request Header Or Cookie Too Large"。那是请求头错误、不是风控，退避重试
    # 永远修不好，反而因为失败响应继续下发挑战 cookie 而正反馈恶化。
    # 2026-07-29 生产实测：累积到 123 个 cookie / 8207 字节时已贴到阈值，再加 40 个
    # 即稳定复现该 400；只留这 5 个（309 字节）时接口一切正常。
    ESSENTIAL_COOKIE_KEYS = frozenset({"UID", "CID", "SEID", "KID", "acw_tc"})

    def __init__(self, cookies: str, *, user_agent: str | None = None) -> None:
        if not cookies or "UID=" not in cookies:
            raise Cloud115AuthError("cookies missing or has no UID field")
        self._cookies: dict[str, str] = self.keep_essential_cookies(
            self.parse_cookies(cookies)
        )
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
    def keep_essential_cookies(cls, cookies_dict: dict[str, str]) -> dict[str, str]:
        """按 ESSENTIAL_COOKIE_KEYS 过滤，保持原有顺序。"""
        return {
            key: value
            for key, value in cookies_dict.items()
            if key in cls.ESSENTIAL_COOKIE_KEYS
        }

    @classmethod
    def prune_cookies(cls, cookies: str) -> tuple[str, int]:
        """裁掉已落库 cookie 串里的非必需项，返回 (裁剪后的串, 丢弃条数)。

        供保活任务修复历史积累的挑战 cookie；不校验 UID，纯字符串加工。
        """
        parsed = cls.parse_cookies(cookies)
        kept = cls.keep_essential_cookies(parsed)
        rendered = "; ".join(f"{key}={value}" for key, value in kept.items())
        return rendered, len(parsed) - len(kept)

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
        next_cookies = self.keep_essential_cookies(self.parse_cookies(cookies))
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
            # 只收会话必需的键：WAF 挑战 cookie 一律丢弃，否则 Cookie 头会一直涨到
            # 越过 nginx 8KB 上限（详见 ESSENTIAL_COOKIE_KEYS 注释）。
            if key not in self.ESSENTIAL_COOKIE_KEYS:
                continue
            if value == "" or value == '""':
                self._cookies.pop(key, None)
            else:
                self._cookies[key] = value.strip()
