"""Provider-owned download client configuration."""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import DownloadClient, DownloadTask, IndexerDownloadClient
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    DownloadClientHandle,
    ProviderDiagnosticCheck,
    ProviderDiagnosticReport,
    ProviderOperationError,
    ProviderUnavailableError,
)
from src.schema.transfers.downloads import (
    DownloadClientCreateRequest,
    DownloadClientDiagnosticResource,
    DownloadClientResource,
    DownloadClientTestRequest,
    DownloadClientUpdateRequest,
)
from src.service.transfers.downloads.common import (
    library_handle_for,
    require_client,
    require_library,
)


def _provider_error(exc: ProviderOperationError) -> ApiError:
    status = {
        "invalid_config": 422,
        "authentication_failed": 401,
        "source_not_found": 404,
        "task_not_managed": 409,
        "unsupported": 422,
        "unavailable": 503,
    }.get(exc.code, 502)
    return ApiError(
        status,
        f"provider_{exc.code}",
        exc.safe_message,
        {"provider_key": exc.provider_key, "operation": exc.operation},
    )


class DownloadClientService:
    @staticmethod
    def _bundle(library):
        try:
            bundle = MEDIA_PROVIDER_REGISTRY.require(library.provider_key)
        except ProviderUnavailableError as exc:
            raise ApiError(
                503,
                "provider_not_installed",
                "媒体提供方未安装",
                {"provider_key": library.provider_key},
            ) from exc
        if bundle.downloads is None:
            raise ApiError(
                422,
                "provider_download_unsupported",
                "该媒体库未提供下载能力",
                {"provider_key": library.provider_key},
            )
        return bundle

    @classmethod
    def _resource(cls, client: DownloadClient) -> DownloadClientResource:
        data = DownloadClientResource.from_model(client)
        try:
            bundle = MEDIA_PROVIDER_REGISTRY.require(client.library.provider_key)
        except ProviderUnavailableError:
            data.provider_config = {}
            return data
        if bundle.downloads is None:
            data.provider_config = {}
            return data
        secret_keys = {
            field.key for field in bundle.downloads.config_fields if field.input == "secret"
        }
        data.provider_config = {
            key: value
            for key, value in (client.provider_config or {}).items()
            if key not in secret_keys
        }
        return data

    @staticmethod
    def _validate_config(
        bundle,
        submitted: object,
        *,
        allow_read_only: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(submitted, dict):
            raise ApiError(
                422,
                "invalid_download_client_provider_config",
                "provider_config must be an object",
            )
        fields = tuple(bundle.downloads.config_fields)
        unknown = sorted(set(submitted) - {field.key for field in fields})
        if unknown:
            raise ApiError(
                422,
                "invalid_download_client_provider_config",
                "provider_config contains unknown fields",
                {"fields": unknown},
            )
        if not allow_read_only:
            read_only = sorted(
                field.key
                for field in bundle.downloads.config_fields
                if field.read_only and field.key in submitted
            )
            if read_only:
                raise ApiError(
                    422,
                    "invalid_download_client_provider_config",
                    "provider_config contains read-only fields",
                    {"fields": read_only},
                )
        return dict(submitted)

    @staticmethod
    def _ensure_name_available(name: str, exclude_client_id: int | None = None) -> None:
        query = DownloadClient.select().where(DownloadClient.name == name)
        if exclude_client_id is not None:
            query = query.where(DownloadClient.id != exclude_client_id)
        if query.exists():
            raise ApiError(
                409,
                "download_client_name_conflict",
                "Download client name already exists",
                {"name": name},
            )

    @classmethod
    def _prepare(cls, bundle, *, library, submitted: dict[str, Any], previous: DownloadClient | None):
        previous_config = dict(previous.provider_config or {}) if previous is not None else {}
        merged = dict(submitted)
        if previous is not None:
            for field in bundle.downloads.config_fields:
                if (
                    field.key not in merged
                    and (field.input == "secret" or field.read_only)
                    and field.key in previous_config
                ):
                    merged[field.key] = previous_config[field.key]
        previous_handle = None
        if previous is not None:
            previous_handle = DownloadClientHandle(
                client_id=previous.id,
                library=library_handle_for(previous.library),
                provider_config=deepcopy(previous.provider_config or {}),
            )
        try:
            prepared = bundle.downloads.prepare_client(
                submitted_config=merged,
                library=library_handle_for(library),
                previous=previous_handle,
            )
        except ProviderOperationError as exc:
            raise _provider_error(exc) from exc
        if not isinstance(prepared, dict):
            raise ApiError(502, "provider_invalid_response", "媒体提供方返回了无效下载配置")
        return dict(prepared)

    @staticmethod
    def _diagnostic_resource(
        report: ProviderDiagnosticReport,
        *,
        started_at: float,
    ) -> DownloadClientDiagnosticResource:
        return DownloadClientDiagnosticResource.model_validate(
            {
                "status": report.status,
                "checks": [
                    {
                        "key": check.key,
                        "status": check.status,
                        "code": check.code,
                        "message": check.message,
                        "details": check.details,
                    }
                    for check in report.checks
                ],
                "checked_at": utc_now_for_db(),
                "elapsed_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
            }
        )

    @classmethod
    def test_client(
        cls,
        payload: DownloadClientTestRequest,
    ) -> DownloadClientDiagnosticResource:
        existing = require_client(payload.client_id) if payload.client_id is not None else None
        library = require_library(payload.library_id)
        if existing is not None and existing.library_id != library.id:
            raise ApiError(
                422,
                "download_client_test_library_mismatch",
                "下载器测试必须使用当前下载器绑定的媒体库",
                {"client_id": existing.id, "library_id": library.id},
            )
        bundle = cls._bundle(library)
        submitted = dict(payload.provider_config)
        if existing is not None and not submitted:
            submitted = dict(existing.provider_config or {})
        prepared = cls._prepare(
            bundle,
            library=library,
            submitted=cls._validate_config(bundle, submitted),
            previous=existing,
        )
        started_at = time.perf_counter()
        try:
            report = bundle.downloads.test_client(
                submitted_config=prepared,
                library=library_handle_for(library),
            )
        except ProviderOperationError as exc:
            report = ProviderDiagnosticReport(
                status="failed",
                checks=(
                    ProviderDiagnosticCheck(
                        key="provider",
                        status="failed",
                        code=exc.code,
                        message=exc.safe_message,
                    ),
                ),
            )
        except Exception:
            report = ProviderDiagnosticReport(
                status="failed",
                checks=(
                    ProviderDiagnosticCheck(
                        key="provider",
                        status="failed",
                        code="provider_test_failed",
                        message="下载器测试失败",
                    ),
                ),
            )
        if not isinstance(report, ProviderDiagnosticReport):
            raise ApiError(502, "provider_invalid_response", "媒体提供方返回了无效测试结果")
        return cls._diagnostic_resource(report, started_at=started_at)

    @classmethod
    def list_clients(cls) -> list[DownloadClientResource]:
        clients = list(
            DownloadClient.select().order_by(
                DownloadClient.created_at.desc(), DownloadClient.id.desc()
            )
        )
        return [cls._resource(client) for client in clients]

    @classmethod
    def create_client(cls, payload: DownloadClientCreateRequest) -> DownloadClientResource:
        name = (payload.name or "").strip()
        if not name:
            raise ApiError(422, "invalid_download_client_name", "Download client name cannot be empty")
        library = require_library(payload.library_id)
        bundle = cls._bundle(library)
        submitted = cls._validate_config(bundle, payload.provider_config)
        provider_config = cls._prepare(bundle, library=library, submitted=submitted, previous=None)
        cls._ensure_name_available(name)
        client = DownloadClient.create(name=name, library=library, provider_config=provider_config)
        return cls._resource(client)

    @classmethod
    def update_client(cls, client_id: int, payload: DownloadClientUpdateRequest) -> DownloadClientResource:
        client = require_client(client_id)
        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            raise ApiError(422, "empty_download_client_update", "At least one field must be provided")
        if "name" in update_data and update_data["name"] is not None:
            name = str(update_data["name"]).strip()
            if not name:
                raise ApiError(422, "invalid_download_client_name", "Download client name cannot be empty")
            if name != client.name:
                cls._ensure_name_available(name, exclude_client_id=client.id)
            client.name = name
        library = client.library
        if update_data.get("library_id") is not None:
            requested_library_id = int(update_data["library_id"])
            if requested_library_id != client.library_id and DownloadTask.select().where(
                DownloadTask.client == client.id
            ).exists():
                raise ApiError(
                    409,
                    "download_client_library_change_forbidden",
                    "Download client library cannot change while tasks exist",
                    {"client_id": client.id},
                )
            library = require_library(requested_library_id)
        if "library_id" in update_data or "provider_config" in update_data:
            bundle = cls._bundle(library)
            config_submitted = update_data.get("provider_config") is not None
            if "provider_config" in update_data and not config_submitted:
                raise ApiError(
                    422,
                    "invalid_download_client_provider_config",
                    "provider_config must be an object",
                )
            submitted = (
                dict(update_data["provider_config"])
                if config_submitted and update_data["provider_config"] is not None
                else dict(client.provider_config or {})
            )
            client.provider_config = cls._prepare(
                bundle,
                library=library,
                submitted=cls._validate_config(
                    bundle,
                    submitted,
                    allow_read_only=not config_submitted,
                ),
                previous=client,
            )
            client.library = library
        client.save()
        return cls._resource(client)

    @classmethod
    def delete_client(cls, client_id: int) -> None:
        client = require_client(client_id)
        if DownloadTask.select().where(DownloadTask.client == client.id).exists():
            raise ApiError(
                409,
                "download_client_in_use",
                "Download client is still referenced by download tasks",
                {"client_id": client.id},
            )
        if IndexerDownloadClient.select().where(
            IndexerDownloadClient.download_client == client.id
        ).exists():
            raise ApiError(
                409,
                "download_client_in_use_by_indexers",
                "Download client is still referenced by indexers",
                {"client_id": client.id},
            )
        client.delete_instance()
