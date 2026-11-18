"""请求级语言协商中间件。

只解析 ``Accept-Language`` 并写入请求级 LanguageState；用户偏好由认证依赖
回填。中间件不改写响应体，SSE 仍可逐块透传；只在响应头追加
``Vary: Accept-Language``，避免共享缓存把不同语言的错误/插件文案混用。
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.i18n.locale import (
    new_language_state,
    reset_language_state,
    set_language_state,
)


class LanguageContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_accept_language = headers.get(b"accept-language")
        accept_language = (
            raw_accept_language.decode("latin-1") if raw_accept_language else None
        )
        state = new_language_state(accept_language)
        token = set_language_state(state)

        async def send_with_vary(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": _with_accept_language_vary(
                        list(message.get("headers") or [])
                    ),
                }
            await send(message)

        try:
            await self.app(scope, receive, send_with_vary)
        finally:
            reset_language_state(token)


def _with_accept_language_vary(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    existing = next(
        (value for name, value in headers if name.lower() == b"vary"), None
    )
    if existing is None:
        return [*headers, (b"vary", b"Accept-Language")]

    values = [value.strip() for value in existing.decode("latin-1").split(",")]
    if any(value.lower() == "accept-language" for value in values):
        return headers
    merged = ", ".join([*[value for value in values if value], "Accept-Language"])
    return [
        (name, value)
        for name, value in headers
        if name.lower() != b"vary"
    ] + [(b"vary", merged.encode("latin-1"))]
