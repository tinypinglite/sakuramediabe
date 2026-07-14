import base64

from src.api.exception.errors import ApiError
from src.lib.cloud115 import Cloud115Error, Cloud115QrLogin, QrStatus
from src.lib.cloud115.qrlogin import APPS
from src.schema.playback.cloud115_libraries import (
    Cloud115QrStatusRequest,
    Cloud115QrStatusResource,
    Cloud115QrTokenResource,
)

# 服务端 QrStatus 枚举 → 前端友好的小写字符串。
_STATUS_TO_STRING: dict[QrStatus, str] = {
    QrStatus.WAITING: "waiting",
    QrStatus.SCANNED: "scanned",
    QrStatus.CONFIRMED: "confirmed",
    QrStatus.EXPIRED: "expired",
    QrStatus.CANCELED: "canceled",
}


class Cloud115QrLoginService:
    """扫码登录服务：token/长轮询两步。

    每个端点独立开一次 SDK 会话（无状态原语），业务层不做 session 落库。
    最后一步的 fetch_result 由 MediaLibraryService.create_cloud115_library 负责，
    因为要把 cookies 落进 MediaLibrary。
    """

    @classmethod
    async def get_token(cls) -> Cloud115QrTokenResource:
        """拿扫码 uid/time/sign + 二维码 PNG（base64 塞进 JSON body）。"""
        try:
            async with Cloud115QrLogin() as qr:
                token = await qr.get_token()
                image = await qr.get_qrcode_image(token.uid)
        except Cloud115Error as exc:
            from src.service.playback.cloud115_backend_service import map_cloud115_error

            raise map_cloud115_error(exc) from exc
        return Cloud115QrTokenResource(
            uid=token.uid,
            time=token.time,
            sign=token.sign,
            qrcode_png_base64=base64.b64encode(image).decode("ascii"),
        )

    @classmethod
    async def poll_status(
        cls, payload: Cloud115QrStatusRequest
    ) -> Cloud115QrStatusResource:
        """长轮询一次（阻塞 ~30s）。上层拿到 waiting/scanned 继续 poll，confirmed 进创建。"""
        from src.lib.cloud115.types import QrCodeToken

        token = QrCodeToken(
            uid=payload.uid,
            time=payload.time,
            sign=payload.sign,
            qrcode="",  # get_qrcode_status 不用这字段
        )
        try:
            async with Cloud115QrLogin() as qr:
                status = await qr.get_qrcode_status(token)
        except Cloud115Error as exc:
            from src.service.playback.cloud115_backend_service import map_cloud115_error

            raise map_cloud115_error(exc) from exc
        return Cloud115QrStatusResource(status=_STATUS_TO_STRING[status])

    @staticmethod
    def validate_app(app: str) -> str:
        """校验 app 是 115 登录槽白名单里的值；用于创建端点的参数校验。"""
        if app not in APPS:
            raise ApiError(
                422,
                "invalid_cloud115_app",
                f"Unsupported 115 login slot: {app!r}",
                {"app": app, "allowed": list(APPS)},
            )
        return app
