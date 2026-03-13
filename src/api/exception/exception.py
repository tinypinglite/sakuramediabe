#!/usr/bin/env python
# -*- coding: utf-8 -*-


from fastapi import status
from fastapi.encoders import jsonable_encoder
from loguru import logger
from starlette.responses import JSONResponse

from src.api.exception.errors import ApiError


def _error_response(status_code: int, code: str, message: str, details=None):
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
            }
        },
    )


async def api_error_handler(request, exc: ApiError):
    return _error_response(exc.status_code, exc.code, exc.message, exc.details)


async def http_exception_handler(request, exc):
    if exc.status_code == 401:
        return _error_response(401, "unauthorized", str(exc.detail))
    if exc.status_code == 403:
        return _error_response(403, "forbidden", str(exc.detail))
    return _error_response(
        exc.status_code,
        "http_error",
        str(exc.detail),
    )


async def validation_exception_handler(request, exc):
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "validation_error",
        "Request validation failed",
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
            exc.message,
            exc.details,
        )
    return _error_response(500, "internal_error", "Internal server error")
