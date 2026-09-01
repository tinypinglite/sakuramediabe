"""Host-facing media provider bundle protocol (v2).

Provider plugins may import these types without importing host models, services,
or a concrete provider implementation.  The bundle is intentionally opaque to
the host: refs and provider configuration are only stored and passed back.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias

from starlette.requests import Request
from starlette.responses import Response

JsonObject: TypeAlias = dict[str, Any]
PlaybackDelivery: TypeAlias = Literal["proxy", "redirect"]
MergedPlaybackFormat: TypeAlias = Literal["mp4", "hls"]
MEDIA_PROVIDER_EXTENSION_KEY = "media.provider"


@dataclass(frozen=True)
class LibraryHandle:
    library_id: int
    provider_key: str
    provider_config: JsonObject
    account_key: str | None


@dataclass(frozen=True)
class MediaHandle:
    media_id: int
    library: LibraryHandle
    storage_ref: JsonObject
    file_name: str
    file_size_bytes: int
    duration_seconds: int


@dataclass(frozen=True)
class DownloadClientHandle:
    client_id: int
    library: LibraryHandle
    provider_config: JsonObject


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    input: Literal["text", "secret", "path"]
    required: bool
    description: str | None = None
    multiline: bool = False
    read_only: bool = False
    hint: str | None = None


ProviderDiagnosticCheckStatus: TypeAlias = Literal["ok", "warning", "failed", "skipped"]
ProviderDiagnosticStatus: TypeAlias = Literal["ok", "warning", "failed"]


@dataclass(frozen=True)
class ProviderDiagnosticCheck:
    key: str
    status: ProviderDiagnosticCheckStatus
    code: str
    message: str
    details: JsonObject | None = None


@dataclass(frozen=True)
class ProviderDiagnosticReport:
    status: ProviderDiagnosticStatus
    checks: tuple[ProviderDiagnosticCheck, ...]


@dataclass(frozen=True)
class PreparedLibrary:
    provider_config: JsonObject
    account_key: str | None


@dataclass(frozen=True)
class BrowseEntry:
    source_ref: JsonObject
    name: str
    entry_type: Literal["file", "directory"]
    size_bytes: int | None
    modified_at: datetime | None
    is_video: bool


@dataclass(frozen=True)
class BrowsePage:
    entries: tuple[BrowseEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ImportFile:
    source_ref: JsonObject
    name: str
    relative_path: str
    size_bytes: int
    is_video: bool


@dataclass(frozen=True)
class ImportFileContent:
    content: bytes
    deletion_receipt: JsonObject


@dataclass(frozen=True)
class ImportPlacement:
    relative_path: str


@dataclass(frozen=True)
class StagedMedia:
    storage_ref: JsonObject
    receipt: JsonObject
    size_bytes: int
    duration_seconds: int | None
    video_info: JsonObject | None


@dataclass(frozen=True)
class PlaybackContext:
    request: Request
    resource_path: str
    delivery: PlaybackDelivery
    url_for: Callable[[str], str]


@dataclass(frozen=True)
class ThumbnailArtifact:
    offset_seconds: int
    relative_path: str


@dataclass(frozen=True)
class ThumbnailGeneration:
    expected_count: int
    artifacts: tuple[ThumbnailArtifact, ...]


class ThumbnailGenerationDeferred(RuntimeError):
    """The source exists but is not ready for thumbnail generation yet."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        max_deferred_attempts: int,
        deferred_backoff_base_seconds: int,
    ) -> None:
        self.error_code = error_code
        self.max_deferred_attempts = max_deferred_attempts
        self.deferred_backoff_base_seconds = deferred_backoff_base_seconds
        super().__init__(message)


class ThumbnailBackendUnavailable(RuntimeError):
    """A library-scoped thumbnail backend failure; do not fail each media."""

    def __init__(self, message: str, *, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(message)


@dataclass(frozen=True)
class ClipArtifact:
    relative_path: str


@dataclass(frozen=True)
class DownloadSubmission:
    source_uri: str
    display_name: str


@dataclass(frozen=True)
class RemoteDownloadTask:
    remote_id: str
    name: str
    state: Literal["queued", "downloading", "completed", "failed"]
    progress: float
    completed_source_ref: JsonObject | None

    def __post_init__(self) -> None:
        if not isinstance(self.remote_id, str) or not self.remote_id.strip():
            raise ValueError("remote_id must be a non-empty string")
        if self.state not in {"queued", "downloading", "completed", "failed"}:
            raise ValueError(f"unsupported download state: {self.state}")
        if not 0 <= self.progress <= 1:
            raise ValueError("download progress must be between 0 and 1")
        if self.state == "completed" and (
            not isinstance(self.completed_source_ref, dict) or not self.completed_source_ref
        ):
            raise ValueError("completed download must include a non-empty completed_source_ref")
        if self.state != "completed" and self.completed_source_ref is not None:
            raise ValueError("only completed download may include completed_source_ref")


class ProviderOperationError(RuntimeError):
    """Safe, structured error exposed by a provider operation."""

    provider_key: str
    operation: str
    code: Literal[
        "invalid_config",
        "authentication_failed",
        "source_not_found",
        "task_not_managed",
        "source_blacklisted",
        "unsupported",
        "unavailable",
    ]
    safe_message: str
    retryable: bool

    def __init__(
        self,
        provider_key: str,
        operation: str,
        code: Literal[
            "invalid_config",
            "authentication_failed",
            "source_not_found",
            "task_not_managed",
            "source_blacklisted",
            "unsupported",
            "unavailable",
        ],
        safe_message: str,
        retryable: bool,
    ) -> None:
        if not isinstance(code, str) or code not in {
            "invalid_config",
            "authentication_failed",
            "source_not_found",
            "task_not_managed",
            "source_blacklisted",
            "unsupported",
            "unavailable",
        }:
            raise ValueError(f"unsupported provider operation error code: {code!r}")
        self.provider_key = provider_key
        self.operation = operation
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        super().__init__(safe_message)


class DownloadProvider(Protocol):
    def submit(self, *, submission: DownloadSubmission) -> RemoteDownloadTask: ...

    def list_tasks(self) -> tuple[RemoteDownloadTask, ...]: ...

    def delete_task(self, *, remote_id: str, delete_files: bool) -> None: ...


class StorageProvider(Protocol):
    def browse(
        self, *, parent_ref: JsonObject | None, cursor: str | None, limit: int
    ) -> BrowsePage: ...

    def scan_import_source(self, *, source_ref: JsonObject) -> Iterable[ImportFile]: ...

    def read_import_file(self, *, source: ImportFile) -> ImportFileContent: ...

    def delete_import_file(self, *, receipt: JsonObject) -> None: ...

    def stage_import_file(
        self,
        *,
        source: ImportFile,
        placement: ImportPlacement,
        source_disposition: Literal["keep", "delete_after_commit"],
        operation_key: str,
    ) -> StagedMedia: ...

    def finalize_import(self, *, receipt: JsonObject) -> None: ...

    def abort_import(self, *, receipt: JsonObject) -> None: ...

    def delete_media(self, *, media: MediaHandle) -> None: ...

    def compute_file_hash(self, *, media: MediaHandle) -> str: ...

    async def handle_playback(
        self, *, media: MediaHandle, context: PlaybackContext
    ) -> Response: ...

    def generate_thumbnails(
        self, *, media: MediaHandle, workspace: Path
    ) -> ThumbnailGeneration: ...

    def create_clip(
        self,
        *,
        media: MediaHandle,
        start_offset_seconds: int,
        end_offset_seconds: int,
        workspace: Path,
    ) -> ClipArtifact: ...


class StorageMergedPlaybackProvider(Protocol):
    """Optional capability for playing ordered media parts as one stream."""

    async def handle_merged_playback(
        self,
        *,
        medias: tuple[MediaHandle, ...],
        context: PlaybackContext,
    ) -> Response: ...


class MediaProviderMergedPlaybackBundle(Protocol):
    """Optional bundle declaration paired with ``StorageMergedPlaybackProvider``."""

    merged_playback_format: MergedPlaybackFormat


class StorageMediaRefScanner(Protocol):
    """Optional capability for enumerating provider-native media refs."""

    def scan_media_refs(self, *, source_ref: JsonObject) -> Iterable[JsonObject]: ...


class StorageCoverSourceProvider(Protocol):
    """Optional capability for opening a video source used to generate its cover."""

    def open_cover_source(self, *, media: MediaHandle) -> AbstractContextManager[Any]: ...


class StorageDurationProbeProvider(Protocol):
    """Optional capability for resolving the duration of an existing media file."""

    def probe_duration_seconds(self, *, media: MediaHandle) -> int: ...


class DownloadComponent(Protocol):
    config_fields: tuple[ConfigField, ...]

    def prepare_client(
        self,
        *,
        submitted_config: JsonObject,
        library: LibraryHandle,
        previous: DownloadClientHandle | None,
    ) -> JsonObject: ...

    def test_client(
        self,
        *,
        submitted_config: JsonObject,
        library: LibraryHandle,
    ) -> ProviderDiagnosticReport: ...

    def build(self, *, client: DownloadClientHandle) -> DownloadProvider: ...


class MediaProviderBundle(Protocol):
    provider_key: str
    display_name: str
    library_config_fields: tuple[ConfigField, ...]
    playback_deliveries: tuple[PlaybackDelivery, ...]
    downloads: DownloadComponent | None

    def prepare_library(
        self,
        *,
        submitted_config: JsonObject,
        previous: LibraryHandle | None,
    ) -> PreparedLibrary: ...

    def build_storage(self, *, library: LibraryHandle) -> StorageProvider: ...


STORAGE_PROVIDER_METHODS: tuple[str, ...] = (
    "browse",
    "scan_import_source",
    "read_import_file",
    "delete_import_file",
    "stage_import_file",
    "finalize_import",
    "abort_import",
    "delete_media",
    "compute_file_hash",
    "handle_playback",
    "generate_thumbnails",
    "create_clip",
)
DOWNLOAD_PROVIDER_METHODS: tuple[str, ...] = (
    "submit",
    "list_tasks",
    "delete_task",
)


def _validate_storage_provider(provider: object) -> StorageProvider:
    _require_methods(provider, STORAGE_PROVIDER_METHODS, "StorageProvider")
    return provider  # type: ignore[return-value]


def _validate_download_provider(provider: object) -> DownloadProvider:
    _require_methods(provider, DOWNLOAD_PROVIDER_METHODS, "DownloadProvider")
    return provider  # type: ignore[return-value]


def _require_methods(value: object, methods: Iterable[str], kind: str) -> None:
    missing = [name for name in methods if not callable(getattr(value, name, None))]
    if missing:
        raise TypeError(f"{kind} 缺少必需操作: {', '.join(missing)}")


class ProviderUnavailableError(LookupError):
    """No active bundle is registered for a provider key."""


class MediaProviderRegistry:
    """Startup registry keyed solely by ``provider_key``."""

    def __init__(self) -> None:
        self._bundles: dict[str, tuple[str, MediaProviderBundle]] = {}

    def require(self, provider_key: str) -> MediaProviderBundle:
        entry = self._bundles.get(provider_key)
        bundle = entry[1] if entry is not None else None
        if bundle is None:
            raise ProviderUnavailableError(provider_key)
        return bundle

    def list_bundles(self) -> tuple[MediaProviderBundle, ...]:
        """Return the active bundles in stable provider-key order."""
        return tuple(
            bundle
            for _plugin_id, bundle in sorted(
                self._bundles.values(), key=lambda item: item[1].provider_key
            )
        )

    def provider_keys_for_plugin(self, plugin_id: str) -> tuple[str, ...]:
        """Return active media-provider keys owned by one plugin."""
        return tuple(
            provider_key
            for provider_key, (owner_plugin_id, _bundle) in sorted(
                self._bundles.items()
            )
            if owner_plugin_id == plugin_id
        )

    def replace(self, registrations: Iterable[Any]) -> set[str]:
        """Build the startup provider table and return conflicting plugin IDs."""
        bundles: dict[str, tuple[str, MediaProviderBundle]] = {}
        rejected: set[str] = set()
        for registration in registrations:
            for extension in registration.extensions:
                if extension.key != MEDIA_PROVIDER_EXTENSION_KEY:
                    continue
                bundle = extension.data
                provider_key = bundle.provider_key
                if provider_key in bundles:
                    rejected.add(registration.plugin_id)
                else:
                    bundles[provider_key] = (registration.plugin_id, bundle)
        self._bundles = bundles
        return rejected

    def storage_for(self, library: LibraryHandle) -> StorageProvider:
        bundle = self.require(library.provider_key)
        return _validate_storage_provider(bundle.build_storage(library=library))

    def download_for(self, client: DownloadClientHandle) -> DownloadProvider:
        bundle = self.require(client.library.provider_key)
        if bundle.downloads is None:
            raise ProviderOperationError(
                provider_key=client.library.provider_key,
                operation="download",
                code="unsupported",
                safe_message="该媒体库未提供下载能力",
                retryable=False,
            )
        return _validate_download_provider(bundle.downloads.build(client=client))


MEDIA_PROVIDER_REGISTRY = MediaProviderRegistry()


def refresh_media_provider_registry(
    registrations: Iterable[Any],
) -> set[str]:
    return MEDIA_PROVIDER_REGISTRY.replace(registrations)


__all__ = [
    "MEDIA_PROVIDER_EXTENSION_KEY",
    "MEDIA_PROVIDER_REGISTRY",
    "BrowseEntry",
    "BrowsePage",
    "ClipArtifact",
    "ConfigField",
    "DownloadClientHandle",
    "DownloadComponent",
    "DownloadProvider",
    "DownloadSubmission",
    "ImportFile",
    "ImportFileContent",
    "ImportPlacement",
    "JsonObject",
    "LibraryHandle",
    "MediaHandle",
    "MediaProviderBundle",
    "MediaProviderRegistry",
    "PlaybackContext",
    "PlaybackDelivery",
    "PreparedLibrary",
    "ProviderDiagnosticCheck",
    "ProviderDiagnosticReport",
    "ProviderOperationError",
    "ProviderUnavailableError",
    "RemoteDownloadTask",
    "StagedMedia",
    "StorageDurationProbeProvider",
    "StorageProvider",
    "ThumbnailArtifact",
    "ThumbnailGeneration",
    "refresh_media_provider_registry",
]
