"""Opaque provider source browsing."""

from src.api.exception.errors import ApiError
from src.common.media_formats import is_supported_video_file_name
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    BrowsePage,
    ProviderOperationError,
    ProviderUnavailableError,
)
from src.schema.transfers.media_import import (
    ImportBrowseEntryResource,
    ImportBrowseRequest,
    ImportBrowseResponse,
)
from src.service.transfers.downloads.common import library_handle_for, require_library


class ProviderBrowseService:
    @classmethod
    def browse(cls, payload: ImportBrowseRequest) -> ImportBrowseResponse:
        library = require_library(payload.library_id)
        try:
            storage = MEDIA_PROVIDER_REGISTRY.storage_for(library_handle_for(library))
            page = storage.browse(
                parent_ref=payload.parent_ref,
                cursor=payload.cursor,
                limit=payload.limit,
            )
        except ProviderUnavailableError as exc:
            raise ApiError(
                503,
                "provider_not_installed",
                "媒体提供方未安装",
                {"provider_key": library.provider_key},
            ) from exc
        except ProviderOperationError as exc:
            status = {
                "invalid_config": 422,
                "authentication_failed": 401,
                "source_not_found": 404,
                "unsupported": 422,
                "unavailable": 503,
            }.get(exc.code, 502)
            raise ApiError(
                status,
                f"provider_{exc.code}",
                exc.safe_message,
                {"provider_key": exc.provider_key, "operation": exc.operation},
            ) from exc
        except Exception as exc:
            raise ApiError(502, "provider_browse_failed", "媒体提供方浏览失败") from exc
        if not isinstance(page, BrowsePage):
            raise ApiError(502, "provider_invalid_response", "媒体提供方返回了无效浏览结果")
        return ImportBrowseResponse(
            library_id=library.id,
            entries=[
                ImportBrowseEntryResource(
                    source_ref=entry.source_ref,
                    name=entry.name,
                    entry_type=entry.entry_type,
                    size_bytes=entry.size_bytes,
                    modified_at=entry.modified_at,
                    is_video=(
                        entry.entry_type == "file"
                        and is_supported_video_file_name(entry.name)
                    ),
                )
                for entry in page.entries
            ],
            next_cursor=page.next_cursor,
        )
