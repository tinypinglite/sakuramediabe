from loguru import logger

from src.lib.cloud115.capabilities.base import Cloud115Capability
from src.lib.cloud115.types import Cloud115CookieStatus


class AuthCapability(Cloud115Capability):
    BASE_MY = "https://my.115.com"

    async def probe_cookies_status(self) -> Cloud115CookieStatus:
        """探测登录态，并区分明确失效与临时上游不可用。"""
        url = f"{self.BASE_MY}/"
        try:
            response = await self._transport.request_raw(
                "GET", url, params={"ct": "guide", "ac": "status"}
            )
        except Exception as exc:
            logger.debug("probe_cookies_status request failed: {}", exc)
            return Cloud115CookieStatus.UNAVAILABLE
        if response.status_code in (302, 401, 403):
            return Cloud115CookieStatus.EXPIRED
        if response.status_code != 200:
            return Cloud115CookieStatus.UNAVAILABLE
        try:
            data = response.json()
        except Exception:
            return Cloud115CookieStatus.UNAVAILABLE
        if not isinstance(data, dict) or "state" not in data:
            return Cloud115CookieStatus.UNAVAILABLE
        if data["state"] is True:
            return Cloud115CookieStatus.ALIVE
        if data["state"] is False:
            return Cloud115CookieStatus.EXPIRED
        return Cloud115CookieStatus.UNAVAILABLE

    async def check_cookies_alive(self) -> bool:
        return await self.probe_cookies_status() is Cloud115CookieStatus.ALIVE
