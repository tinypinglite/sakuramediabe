"""115 扫码登录（获取 cookies）。

独立于 Cloud115Client——登录发生在拿到 cookies **之前**，所以本类不需要 cookies。
产出的 QrLoginResult.cookies 可直接喂 `Cloud115Client(cookies=...)`。

4 步原语（真机验证 2026-07-12）：
    1. get_token()               -> QrCodeToken(uid/time/sign/qrcode)
    2. get_qrcode_image(uid)     -> PNG bytes（或自行渲染 token.qrcode）
    3. get_qrcode_status(token)  -> QrStatus（长轮询，无变化时阻塞 ~30s 返回 WAITING）
    4. fetch_result(uid, app)    -> QrLoginResult（含可直接用的 cookies 串）

选择/编排由上层做（展示二维码 + 轮询 + 确认后换 cookie），本类只给原语，不做一步到位封装。

app 决定 cookie 落在哪个"登录槽"。默认 **alipaymini**（支付宝小程序端）：占一个用户平时不用
的槽，不会挤掉手机 / 网页版登录，也不易被踢——适合给后台服务长期绑定。可用值见 APPS。
（注：选 alipaymini 的价值是"不互踢"，不是"有效期更长"——后者没有依据。）
"""

from __future__ import annotations

import httpx

from src.lib.cloud115.exceptions import Cloud115AuthError, Cloud115RequestError
from src.lib.cloud115.types import QrCodeToken, QrLoginResult, QrStatus

# 可选的登录槽（app）。alipaymini/wechatmini 是支付宝 / 微信小程序端。
APPS: tuple[str, ...] = (
    "web", "android", "ios", "linux", "mac", "windows",
    "tv", "alipaymini", "wechatmini", "qandroid",
)


class Cloud115QrLogin:
    """115 扫码登录客户端（无需 cookies）。

    用法：
        async with Cloud115QrLogin() as qr:
            token = await qr.get_token()
            png = await qr.get_qrcode_image(token.uid)   # 展示给用户扫
            while True:
                st = await qr.get_qrcode_status(token)     # 长轮询
                if st is QrStatus.CONFIRMED:
                    break
                if st in (QrStatus.EXPIRED, QrStatus.CANCELED):
                    raise RuntimeError(st.name)
            result = await qr.fetch_result(token.uid, app="alipaymini")
            # result.cookies 直接喂 Cloud115Client(cookies=result.cookies)
    """

    # token 用 web 端点即可（与最终绑定的 app 无关，app 只在第 4 步生效）
    _TOKEN_URL = "https://qrcodeapi.115.com/api/1.0/web/1.0/token/"
    _IMAGE_URL = "https://qrcodeapi.115.com/api/1.0/mac/1.0/qrcode?uid={uid}"
    _STATUS_URL = "https://qrcodeapi.115.com/get/status/"
    _RESULT_URL = "https://passportapi.115.com/app/1.0/{app}/1.0/login/qrcode/"

    _DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        timeout: float = 35.0,   # 状态接口是长轮询，需 > ~30s
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            follow_redirects=True,
            headers={"User-Agent": user_agent or self._DEFAULT_UA},
        )

    async def __aenter__(self) -> "Cloud115QrLogin":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_token(self) -> QrCodeToken:
        """第 1 步：拿一次扫码会话的 uid/time/sign + 二维码内容。"""
        r = await self._client.get(self._TOKEN_URL)
        data = self._data(r, self._TOKEN_URL)
        uid = str(data.get("uid", ""))
        if not uid:
            raise Cloud115RequestError(
                "qrcode token missing uid",
                method="GET", url=self._TOKEN_URL, detail=r.text[:200],
            )
        return QrCodeToken(
            uid=uid,
            time=int(data.get("time") or 0),
            sign=str(data.get("sign", "")),
            qrcode=str(data.get("qrcode", "")),
        )

    async def get_qrcode_image(self, uid: str) -> bytes:
        """第 2 步：拿 115 现成的二维码 PNG（仅凭 uid，无状态）。也可自行渲染 token.qrcode。"""
        if not uid:
            raise ValueError("uid is required")
        r = await self._client.get(self._IMAGE_URL.format(uid=uid))
        if r.status_code != 200 or not r.content:
            raise Cloud115RequestError(
                f"qrcode image http {r.status_code}",
                method="GET", url=self._IMAGE_URL.format(uid=uid), detail=r.text[:200],
            )
        return r.content

    async def get_qrcode_status(self, token: QrCodeToken) -> QrStatus:
        """第 3 步：长轮询一次扫码状态。

        115 无变化时阻塞 ~30s 后返回空 data -> WAITING；扫码 / 确认 / 过期 / 取消会立即返回。
        上层循环调用即可（WAITING/SCANNED 继续等，CONFIRMED 进第 4 步，EXPIRED/CANCELED 终止）。
        """
        try:
            r = await self._client.get(
                self._STATUS_URL,
                params={"uid": token.uid, "time": token.time, "sign": token.sign},
            )
            data = (r.json() or {}).get("data") or {}
            raw = data.get("status")
        except httpx.TimeoutException:
            return QrStatus.WAITING  # 长轮询超时 = 无变化
        if raw is None:
            return QrStatus.WAITING  # 空 data = 无变化
        try:
            return QrStatus(int(raw))
        except (ValueError, TypeError):
            return QrStatus.WAITING

    async def fetch_result(self, uid: str, app: str = "alipaymini") -> QrLoginResult:
        """第 4 步：换 cookie，并把设备绑到 app 这个登录槽。

        必须在 get_qrcode_status 返回 CONFIRMED 后调用。app 见 APPS，默认 alipaymini。
        """
        if not uid:
            raise ValueError("uid is required")
        if app not in APPS:
            raise ValueError(f"unknown app {app!r}; expected one of {APPS}")
        r = await self._client.post(
            self._RESULT_URL.format(app=app),
            data={"app": app, "account": uid},
        )
        body = r.json() or {}
        data = body.get("data") or {}
        cookie = data.get("cookie") or {}
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie.items())
        if not cookie_str:
            raise Cloud115AuthError(
                f"qrcode login result has no cookie (app={app}): {str(body)[:200]}",
                endpoint=self._RESULT_URL.format(app=app),
            )
        return QrLoginResult(
            cookies=cookie_str,
            cookie_dict=dict(cookie),
            user_id=str(data.get("user_id", "")),
            app=app,
        )

    @staticmethod
    def _data(response: httpx.Response, url: str) -> dict:
        try:
            return (response.json() or {}).get("data") or {}
        except Exception as exc:
            raise Cloud115RequestError(
                f"non-json body on GET {url}", method="GET", url=url, detail=str(exc),
            ) from exc
