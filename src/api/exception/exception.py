from fastapi import status
from fastapi.encoders import jsonable_encoder
from loguru import logger
from starlette.responses import JSONResponse

from src.api.exception.errors import ApiError
from src.i18n.catalog import translate_for_display
from src.i18n.locale import DEFAULT_LANGUAGE, current_language

_STATUS_ERROR_FALLBACKS = {
    400: "Invalid request",
    401: "Please sign in again",
    403: "You do not have permission to perform this action",
    404: "The requested resource was not found",
    409: "The request conflicts with the current state",
    422: "Request validation failed",
}


def _localized_error_message(
    code: str,
    source_message: str,
    status_code: int,
    params: dict | None = None,
) -> str:
    # 默认语言保持源码 message 优先（zh 的多数错误仍是源码中文）；其他语言
    # 走 catalog → en → 状态码兜底，不做 "== en" 的硬编码分支。
    locale = current_language()
    key = f"errors.{code}"
    resolved_params = params or {}
    if locale == DEFAULT_LANGUAGE:
        return (
            translate_for_display(
                key,
                locale=locale,
                default=source_message,
                **resolved_params,
            )
            or source_message
        )
    # 通用 HTTP 错误保留状态专属英文文案（404/400 等），避免被
    # errors.http_error 的通用文案掩盖。
    if code == "http_error":
        status_message = _STATUS_ERROR_FALLBACKS.get(status_code)
        if status_message:
            return status_message
        if status_code >= 500:
            return "Internal server error"
    message = translate_for_display(
        key,
        locale=locale,
        default=None,
        **resolved_params,
    )
    if message is not None:
        return message
    if status_code >= 500:
        return "Internal server error"
    return _STATUS_ERROR_FALLBACKS.get(status_code, "Request failed")


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details=None,
    headers: dict[str, str] | None = None,
):
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
            }
        },
    )


async def api_error_handler(request, exc: ApiError):
    return _error_response(
        exc.status_code,
        exc.code,
        _localized_error_message(
            exc.code, exc.message, exc.status_code, exc.params
        ),
        exc.details,
        exc.response_headers,
    )


async def http_exception_handler(request, exc):
    if exc.status_code == 401:
        return _error_response(
            401,
            "unauthorized",
            _localized_error_message("unauthorized", "请先登录", 401),
        )
    if exc.status_code == 403:
        return _error_response(
            403,
            "forbidden",
            _localized_error_message("forbidden", "没有权限执行此操作", 403),
        )
    return _error_response(
        exc.status_code,
        "http_error",
        _localized_error_message("http_error", str(exc.detail), exc.status_code),
    )


async def validation_exception_handler(request, exc):
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "validation_error",
        _localized_error_message(
            "validation_error",
            "请求参数校验失败",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        ),
        {
            "detail": jsonable_encoder(exc.errors()),
            "body": jsonable_encoder(exc.body),
        },
    )


async def all_exception_handler(request, exc):
    logger.exception(str(exc))
    if isinstance(exc, ApiError):
        return _error_response(
            exc.status_code,
            exc.code,
            _localized_error_message(
                exc.code, exc.message, exc.status_code, exc.params
            ),
            exc.details,
            exc.response_headers,
        )
    return _error_response(
        500,
        "internal_error",
        _localized_error_message("internal_error", "服务器内部错误", 500),
    )
