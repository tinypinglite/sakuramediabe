from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.logs import LogExportRange
from src.service.system.log_export_service import (
    LogEntriesNotFoundError,
    LogExportService,
    LogExportTooLargeError,
    LogsUnavailableError,
)

router = APIRouter(
    prefix="/system/logs",
    tags=["logs"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("/archive")
def export_log_archive(
    range: LogExportRange = Query(default=LogExportRange.LAST_24_HOURS),
    redact_sensitive: bool = Query(default=True),
):
    try:
        archive = LogExportService.build_archive(
            export_range=range,
            redact_sensitive=redact_sensitive,
        )
    except LogsUnavailableError as exc:
        raise ApiError(
            404,
            "logs_unavailable",
            "服务器日志目录不可用",
        ) from exc
    except LogEntriesNotFoundError as exc:
        raise ApiError(
            404,
            "log_entries_not_found",
            "所选时间范围内没有可导出的日志",
        ) from exc
    except LogExportTooLargeError as exc:
        raise ApiError(
            413,
            "log_export_too_large",
            "所选时间范围的日志过大，请缩短时间范围后重试",
        ) from exc

    return StreamingResponse(
        LogExportService.iter_archive_chunks(archive),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{archive.filename}"'
            ),
            "Content-Length": str(archive.size_bytes),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
