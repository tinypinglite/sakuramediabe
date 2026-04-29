"""闭源 metadata provider 授权 service。"""

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from sakuramedia_metadata_providers.exceptions import MetadataLicenseError
from sakuramedia_metadata_providers.license.client import LicenseClient
from sakuramedia_metadata_providers.license.state import LicenseStatus

from src.api.exception.errors import ApiError
from src.metadata.license_runtime import resolve_metadata_provider_license_runtime
from src.schema.system.metadata_provider_license import (
    MetadataProviderLicenseActivateRequest,
    MetadataProviderLicenseConnectivityTestResource,
    MetadataProviderLicenseStatusResource,
)


class MetadataProviderLicenseService:
    LICENSE_CENTER_URL = "https://sakuramedia-license-worker.tinyping.workers.dev/"
    CONNECTIVITY_TEST_TIMEOUT_SECONDS = 10.0
    _activation_lock = threading.RLock()

    BAD_REQUEST_CODES = frozenset({
        "invalid_json",
        "invalid_request",
        "invalid_product",
        "invalid_version",
    })
    FORBIDDEN_CODES = frozenset({
        "activation_code_invalid",
        "activation_code_disabled",
        "activation_code_expired",
        "activation_code_used",
        "license_revoked",
        "license_expired",
        "instance_disabled",
        "instance_deactivated",
        "instance_mismatch",
        "fingerprint_mismatch",
        "instance_limit_exceeded",
        "version_blocked",
        "request_replayed",
        "request_timestamp_invalid",
    })
    CONFLICT_CODES = frozenset({"activation_conflict"})
    RATE_LIMIT_CODES = frozenset({"too_many_requests"})
    BAD_GATEWAY_CODES = frozenset({
        "license_server_error",
        "license_create_failed",
    })

    @classmethod
    def get_status(cls) -> MetadataProviderLicenseStatusResource:
        try:
            status = cls._build_client().status()
        except Exception as exc:
            logger.warning("Metadata provider license status unavailable: {}", exc.__class__.__name__)
            status = LicenseStatus(
                configured=True,
                active=False,
                error_code="license_unavailable",
                message="License state cannot be validated",
            )
        return cls._status_to_resource(status)

    @classmethod
    def test_connectivity(cls) -> MetadataProviderLicenseConnectivityTestResource:
        runtime = resolve_metadata_provider_license_runtime()
        proxy = runtime.license_proxy
        start_at = time.time()
        client_kwargs: dict[str, Any] = {
            "timeout": cls.CONNECTIVITY_TEST_TIMEOUT_SECONDS,
            "trust_env": False,
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        try:
            # 授权中心探测只验证网络/代理可达性，不触发授权状态读写。
            with httpx.Client(**client_kwargs) as client:
                response = client.get(cls.LICENSE_CENTER_URL)
            return MetadataProviderLicenseConnectivityTestResource(
                ok=True,
                url=cls.LICENSE_CENTER_URL,
                proxy_enabled=bool(proxy),
                elapsed_ms=cls._elapsed_ms(start_at),
                status_code=response.status_code,
            )
        except httpx.HTTPError as exc:
            logger.warning("Metadata provider license center connectivity test failed: {}", exc.__class__.__name__)
            return MetadataProviderLicenseConnectivityTestResource(
                ok=False,
                url=cls.LICENSE_CENTER_URL,
                proxy_enabled=bool(proxy),
                elapsed_ms=cls._elapsed_ms(start_at),
                error=cls._sanitize_connectivity_error(exc, proxy),
            )

    @classmethod
    def activate(
        cls,
        payload: MetadataProviderLicenseActivateRequest,
    ) -> MetadataProviderLicenseStatusResource:
        activation_code = payload.activation_code.strip()
        if not activation_code:
            raise ApiError(
                400,
                "invalid_request",
                "activation_code is required",
                {"field": "activation_code"},
            )
        runtime = resolve_metadata_provider_license_runtime()
        state_path = cls._resolve_state_path(runtime.state_path)
        temp_state_path = state_path.with_name(
            f".{state_path.name}.activate-{uuid.uuid4().hex}.tmp"
        )
        try:
            with cls._activation_lock:
                status = LicenseClient(
                    version=runtime.version,
                    state_path=str(temp_state_path),
                    proxy=runtime.license_proxy,
                ).activate(activation_code)
                if not temp_state_path.exists():
                    raise RuntimeError("Temporary provider license state was not written")
                state_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp_state_path, state_path)
        except MetadataLicenseError as exc:
            raise cls._api_error_from_license_error(exc) from exc
        except httpx.HTTPError as exc:
            logger.warning("Metadata provider license service unavailable: {}", exc.__class__.__name__)
            raise ApiError(
                503,
                "license_unavailable",
                "License service is unavailable",
            ) from exc
        except Exception as exc:
            logger.exception("Metadata provider license activation failed")
            raise ApiError(
                503,
                "license_unavailable",
                "License service is unavailable",
            ) from exc
        finally:
            temp_state_path.unlink(missing_ok=True)
        return cls._status_to_resource(status)

    @classmethod
    def renew(cls) -> MetadataProviderLicenseStatusResource:
        try:
            # 手动续租只委托授权客户端，避免业务层拼接授权中心协议细节。
            status = cls._build_client().renew()
        except MetadataLicenseError as exc:
            raise cls._api_error_from_license_error(exc) from exc
        except httpx.HTTPError as exc:
            logger.warning("Metadata provider license service unavailable: {}", exc.__class__.__name__)
            raise ApiError(
                503,
                "license_unavailable",
                "License service is unavailable",
            ) from exc
        except Exception as exc:
            logger.exception("Metadata provider license renewal failed")
            raise ApiError(
                503,
                "license_unavailable",
                "License service is unavailable",
            ) from exc
        return cls._status_to_resource(status)

    @classmethod
    def _build_client(cls) -> LicenseClient:
        runtime = resolve_metadata_provider_license_runtime()
        return LicenseClient(
            version=runtime.version,
            state_path=runtime.state_path,
            proxy=runtime.license_proxy,
        )

    @staticmethod
    def _resolve_state_path(state_path: str) -> Path:
        path = Path(state_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path

    @staticmethod
    def _elapsed_ms(start_at: float) -> int:
        return int((time.time() - start_at) * 1000)

    @staticmethod
    def _sanitize_connectivity_error(exc: httpx.HTTPError, proxy: str | None) -> str:
        message = f"{exc.__class__.__name__}: {str(exc)}"
        if proxy:
            message = message.replace(proxy, "[redacted_proxy]")
        return message

    @classmethod
    def _status_to_resource(cls, status: Any) -> MetadataProviderLicenseStatusResource:
        return MetadataProviderLicenseStatusResource.model_validate(
            status.model_dump(mode="json")
        )

    @classmethod
    def _api_error_from_license_error(cls, exc: MetadataLicenseError) -> ApiError:
        code = exc.code or "license_server_error"
        return ApiError(
            cls._status_code_for_license_error(code),
            code,
            str(exc),
            {"license_error_code": code},
        )

    @classmethod
    def _status_code_for_license_error(cls, code: str) -> int:
        if code in cls.BAD_REQUEST_CODES:
            return 400
        if code in cls.FORBIDDEN_CODES:
            return 403
        if code in cls.CONFLICT_CODES:
            return 409
        if code in cls.RATE_LIMIT_CODES:
            return 429
        if code in cls.BAD_GATEWAY_CODES:
            return 502
        return 502
