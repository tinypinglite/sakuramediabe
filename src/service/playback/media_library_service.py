import threading
import time
from dataclasses import asdict
from typing import Any

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.service_helpers import require_by_id
from src.model import DownloadClient, Media, MediaLibrary
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
    ProviderUnavailableError,
    StorageSpaceUsage,
    supports_space_usage,
)
from src.schema.playback.media_libraries import (
    MediaLibraryCreateRequest,
    MediaLibraryResource,
    MediaLibraryUpdateRequest,
)
from src.service.playback.operation_locks import (
    LIBRARY_LOCK,
    media_operation_lock,
)
from src.service.playback.provider_helpers import library_handle_for

# 状态页每次打开都会查询容量，远程 provider 有网络成本，进程内短暂缓存。
SPACE_USAGE_CACHE_TTL_SECONDS = 300
_SPACE_USAGE_CACHE: dict[str, tuple[float, StorageSpaceUsage]] = {}
_SPACE_USAGE_CACHE_LOCK = threading.Lock()


class MediaLibraryService:
    @staticmethod
    def _require_library(library_id: int) -> MediaLibrary:
        return require_by_id(
            MediaLibrary,
            library_id,
            "media_library",
            error_message="Media library not found",
            error_details_key="library_id",
        )

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_media_library_name",
                "Media library name cannot be empty",
            )
        return normalized

    @staticmethod
    def _bundle(provider_key: str):
        try:
            return MEDIA_PROVIDER_REGISTRY.require(provider_key)
        except ProviderUnavailableError as exc:
            raise ApiError(
                503,
                "provider_not_installed",
                "媒体提供方未安装",
                {"provider_key": provider_key},
            ) from exc

    @classmethod
    def _resource(cls, library: MediaLibrary) -> MediaLibraryResource:
        resource = MediaLibraryResource.from_model(library)
        try:
            bundle = MEDIA_PROVIDER_REGISTRY.require(library.provider_key)
        except ProviderUnavailableError:
            resource.provider_config = {}
            return resource
        secret_keys = {
            field.key for field in bundle.library_config_fields if field.input == "secret"
        }
        resource.provider_config = {
            key: value
            for key, value in (library.provider_config or {}).items()
            if key not in secret_keys
        }
        return resource

    @staticmethod
    def _ensure_name_available(name: str, exclude_library_id: int | None = None) -> None:
        query = MediaLibrary.select().where(MediaLibrary.name == name)
        if exclude_library_id is not None:
            query = query.where(MediaLibrary.id != exclude_library_id)
        if query.exists():
            raise ApiError(
                409,
                "media_library_name_conflict",
                "Media library name already exists",
                {"name": name},
            )

    @staticmethod
    def _submitted_config(payload_config: object) -> dict[str, Any]:
        if not isinstance(payload_config, dict):
            raise ApiError(
                422,
                "invalid_media_library_provider_config",
                "provider_config must be an object",
            )
        return dict(payload_config)

    @classmethod
    def _prepare_config(
        cls,
        bundle,
        submitted_config: dict[str, Any],
        previous_library: MediaLibrary | None,
    ) -> tuple[dict[str, Any], Any]:
        fields = tuple(bundle.library_config_fields)
        field_map = {field.key: field for field in fields}
        unknown = sorted(set(submitted_config) - set(field_map))
        if unknown:
            raise ApiError(
                422,
                "invalid_media_library_provider_config",
                "provider_config contains unknown fields",
                {"fields": unknown},
            )
        read_only = sorted(
            key for key in submitted_config if field_map[key].read_only
        )
        if read_only:
            raise ApiError(
                422,
                "invalid_media_library_provider_config",
                "provider_config contains read-only fields",
                {"fields": read_only},
            )
        previous_config = (
            dict(previous_library.provider_config or {}) if previous_library is not None else {}
        )
        merged = dict(submitted_config)
        if previous_library is not None:
            for field in fields:
                if (
                    field.key not in merged
                    and (field.input == "secret" or field.read_only)
                    and field.key in previous_config
                ):
                    merged[field.key] = previous_config[field.key]
        previous_handle = (
            library_handle_for(previous_library) if previous_library is not None else None
        )
        try:
            prepared = bundle.prepare_library(
                submitted_config=merged,
                previous=previous_handle,
            )
        except ProviderOperationError as exc:
            status_code = {
                "source_not_found": 404,
                "authentication_failed": 401,
                "unavailable": 503,
                "invalid_config": 422,
                "unsupported": 422,
            }[exc.code]
            raise ApiError(
                status_code,
                f"provider_{exc.code}",
                exc.safe_message,
            ) from exc
        if not isinstance(prepared.provider_config, dict):
            raise ApiError(
                502,
                "provider_invalid_response",
                "媒体提供方返回了无效配置",
            )
        return dict(prepared.provider_config), prepared

    @classmethod
    def list_libraries(cls) -> list[MediaLibraryResource]:
        libraries = list(
            MediaLibrary.select().order_by(MediaLibrary.created_at.desc(), MediaLibrary.id.desc())
        )
        return [cls._resource(library) for library in libraries]

    @classmethod
    def storage_space_usages(cls) -> dict[int, StorageSpaceUsage]:
        """按库读取存储端容量；不支持的 provider 与查询失败都不出现在结果里。"""
        usages: dict[int, StorageSpaceUsage] = {}
        for library in MediaLibrary.select():
            usage = cls._space_usage(library)
            if usage is not None:
                usages[library.id] = usage
        return usages

    @classmethod
    def _space_usage(cls, library: MediaLibrary) -> StorageSpaceUsage | None:
        cache_key = cls._space_cache_key(library)
        now = time.monotonic()
        with _SPACE_USAGE_CACHE_LOCK:
            cached = _SPACE_USAGE_CACHE.get(cache_key)
            if cached is not None and now - cached[0] < SPACE_USAGE_CACHE_TTL_SECONDS:
                return cached[1]
        try:
            storage = MEDIA_PROVIDER_REGISTRY.storage_for(library_handle_for(library))
            if not supports_space_usage(storage):
                return None
            usage = storage.get_space_usage()
        except (ProviderOperationError, ProviderUnavailableError):
            return None
        except Exception as exc:
            # 可选指标：插件侧任何异常都不应让 /status/insights 整体失败。
            logger.warning(
                "Storage space query failed library_id={} provider_key={} detail={}",
                library.id,
                library.provider_key,
                exc,
            )
            return None
        if not isinstance(usage, StorageSpaceUsage):
            return None
        with _SPACE_USAGE_CACHE_LOCK:
            _SPACE_USAGE_CACHE[cache_key] = (time.monotonic(), usage)
        return usage

    @staticmethod
    def _space_cache_key(library: MediaLibrary) -> str:
        # 同一 115 账号可能挂多个库，按账号去重，避免重复远程查询。
        scope = library.account_key or f"library:{library.id}"
        return f"{library.provider_key}:{scope}"

    @staticmethod
    def _forget_space_usage(library: MediaLibrary) -> None:
        with _SPACE_USAGE_CACHE_LOCK:
            _SPACE_USAGE_CACHE.pop(MediaLibraryService._space_cache_key(library), None)

    @classmethod
    def list_provider_catalog(cls) -> list[dict[str, Any]]:
        entries = []
        for bundle in MEDIA_PROVIDER_REGISTRY.list_bundles():
            downloads = bundle.downloads
            entries.append(
                {
                    "provider_key": bundle.provider_key,
                    "display_name": bundle.display_name,
                    "library_config_fields": [
                        asdict(field) for field in bundle.library_config_fields
                    ],
                    "playback_deliveries": list(bundle.playback_deliveries),
                    "download_config_fields": (
                        None
                        if downloads is None
                        else [asdict(field) for field in downloads.config_fields]
                    ),
                }
            )
        return entries

    @classmethod
    def create_library(cls, payload: MediaLibraryCreateRequest) -> MediaLibraryResource:
        name = cls._validate_name(payload.name)
        provider_key = payload.provider_key.strip()
        if not provider_key:
            raise ApiError(422, "invalid_media_library_provider", "provider_key cannot be empty")
        bundle = cls._bundle(provider_key)
        provider_config, prepared = cls._prepare_config(
            bundle,
            cls._submitted_config(payload.provider_config),
            None,
        )
        cls._ensure_name_available(name)
        library = MediaLibrary.create(
            name=name,
            provider_key=provider_key,
            provider_config=provider_config,
            account_key=prepared.account_key,
        )
        return cls._resource(library)

    @classmethod
    def update_library(
        cls,
        library_id: int,
        payload: MediaLibraryUpdateRequest,
    ) -> MediaLibraryResource:
        with media_operation_lock(LIBRARY_LOCK, library_id):
            library = cls._require_library(library_id)
            update_data = payload.model_dump(exclude_unset=True, by_alias=False)
            if not update_data:
                raise ApiError(422, "empty_media_library_update", "At least one field must be provided")
            cls._forget_space_usage(library)
            if "name" in update_data and update_data["name"] is not None:
                name = cls._validate_name(update_data["name"])
                if name != library.name:
                    cls._ensure_name_available(name, exclude_library_id=library.id)
                library.name = name
            if "provider_config" in update_data:
                bundle = cls._bundle(library.provider_key)
                provider_config, prepared = cls._prepare_config(
                    bundle,
                    cls._submitted_config(update_data["provider_config"]),
                    library,
                )
                library.provider_config = provider_config
                library.account_key = prepared.account_key
            library.save()
            # provider_config/account_key 可能已变化，新旧缓存键都要失效。
            cls._forget_space_usage(library)
            return cls._resource(library)

    @classmethod
    def delete_library(cls, library_id: int) -> None:
        with media_operation_lock(LIBRARY_LOCK, library_id):
            library = cls._require_library(library_id)
            if (
                Media.select().where(Media.library == library.id).exists()
                or DownloadClient.select().where(DownloadClient.library == library.id).exists()
            ):
                raise ApiError(
                    409,
                    "media_library_in_use",
                    "Media library is still referenced",
                    {"library_id": library.id},
                )
            cls._forget_space_usage(library)
            library.delete_instance()


__all__ = ["MediaLibraryService"]
