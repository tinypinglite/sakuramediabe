from _typeshed import Incomplete
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from starlette.requests import Request
from starlette.responses import Response
from typing import Any, Literal, Protocol, TypeAlias, TypeGuard

MergedPlaybackFormat: TypeAlias = Literal["mp4", "hls"]
ProviderDiagnosticCheckStatus: TypeAlias = Literal["ok", "warning", "failed", "skipped"]
ProviderDiagnosticStatus: TypeAlias = Literal["ok", "warning", "failed"]

__all__ = ['DEFAULT_DOWNLOAD_SOURCE_KINDS', 'DOWNLOAD_SOURCE_KINDS', 'MEDIA_PROVIDER_EXTENSION_KEY', 'MEDIA_PROVIDER_REGISTRY', 'BrowseEntry', 'BrowsePage', 'ClipArtifact', 'ConfigField', 'DownloadClientHandle', 'DownloadComponent', 'DownloadProvider', 'DownloadSourceKind', 'DownloadSubmission', 'ImportFile', 'ImportFileContent', 'ImportPlacement', 'JsonObject', 'LibraryHandle', 'LibraryLoginSpec', 'MediaHandle', 'MediaProviderBundle', 'MediaProviderRegistry', 'MediaTransferReader', 'MediaTransferSource', 'MediaTransferSourceInfo', 'PlaybackContext', 'PlaybackDelivery', 'PreparedLibrary', 'PreparedLibraryLogin', 'ProviderDiagnosticCheck', 'ProviderDiagnosticReport', 'ProviderOperationError', 'ProviderUnavailableError', 'RemoteDownloadTask', 'StagedMedia', 'StagedMediaTransfer', 'StorageDurationProbeProvider', 'StorageImportSourceIdentityProvider', 'StorageImportSourceValidationProvider', 'StorageManagedMediaRefScanner', 'StorageMediaTransferSourceCleanupProvider', 'StorageMediaTransferSourceProvider', 'StorageMediaTransferTargetProvider', 'StorageMergedPlaybackPreflightProvider', 'StorageProvider', 'StorageResolutionProbeProvider', 'StorageSpaceProvider', 'StorageSpaceUsage', 'StorageVideoInfoProbeProvider', 'ThumbnailArtifact', 'ThumbnailGeneration', 'WebLoginCookieCapture', 'WebLoginLocalStorageCapture', 'refresh_media_provider_registry', 'supports_import_source_validation', 'supports_media_transfer_source', 'supports_media_transfer_source_cleanup', 'supports_media_transfer_target', 'supports_space_usage']

JsonObject: TypeAlias = dict[str, Any]
ScanProgressCallback: TypeAlias = Callable[[JsonObject], None]
PlaybackDelivery: TypeAlias = Literal["proxy", "redirect"]
DownloadSourceKind: TypeAlias = Literal["magnet", "torrent", "ed2k", "http"]
DOWNLOAD_SOURCE_KINDS: frozenset[DownloadSourceKind]
DEFAULT_DOWNLOAD_SOURCE_KINDS: frozenset[DownloadSourceKind]
MEDIA_PROVIDER_EXTENSION_KEY: str

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
    content_md5: str | None = ...
    content_sha1: str | None = ...
    content_gcid: str | None = ...

@dataclass(frozen=True)
class DownloadClientHandle:
    client_id: int
    library: LibraryHandle
    provider_config: JsonObject

@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    input: Literal['text', 'secret', 'path']
    required: bool
    description: str | None = ...
    multiline: bool = ...
    read_only: bool = ...
    hint: str | None = ...
    label_i18n: dict[str, str] | None = ...
    description_i18n: dict[str, str] | None = ...
    hint_i18n: dict[str, str] | None = ...

@dataclass(frozen=True)
class ProviderDiagnosticCheck:
    key: str
    status: ProviderDiagnosticCheckStatus
    code: str
    message: str
    details: JsonObject | None = ...

@dataclass(frozen=True)
class ProviderDiagnosticReport:
    status: ProviderDiagnosticStatus
    checks: tuple[ProviderDiagnosticCheck, ...]

@dataclass(frozen=True)
class PreparedLibrary:
    provider_config: JsonObject
    account_key: str | None

@dataclass(frozen=True)
class WebLoginCookieCapture:
    url: str
    capture_names: tuple[str, ...]
    required_names: tuple[str, ...] = ...

@dataclass(frozen=True)
class WebLoginLocalStorageCapture:
    origin: str
    required_json_keys: tuple[str, ...] = ...

@dataclass(frozen=True)
class LibraryLoginSpec:
    start_url: str
    allowed_hosts: tuple[str, ...]
    cookie_captures: tuple[WebLoginCookieCapture, ...] = ...
    local_storage_captures: tuple[WebLoginLocalStorageCapture, ...] = ...
    managed_config_keys: tuple[str, ...] = ...
    timeout_seconds: int = ...
    mode: Literal['webview'] = ...

@dataclass(frozen=True)
class PreparedLibraryLogin:
    provider_config: JsonObject
    account_key: str | None
    account_label: str | None = ...

@dataclass(frozen=True)
class BrowseEntry:
    source_ref: JsonObject
    name: str
    entry_type: Literal['file', 'directory']
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
    resolution: str | None = ...

@dataclass(frozen=True)
class MediaTransferSourceInfo:
    file_name: str
    size_bytes: int
    content_md5: str | None = ...
    content_sha1: str | None = ...
    content_gcid: str | None = ...

class MediaTransferReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...
    def seek(self, offset: int, whence: int = 0) -> int: ...

class MediaTransferSource(Protocol):
    info: MediaTransferSourceInfo
    def open_reader(self) -> AbstractContextManager[MediaTransferReader]: ...
    def assert_unchanged(self) -> None: ...

@dataclass(frozen=True)
class StagedMediaTransfer:
    status: Literal['staged', 'not_available']
    storage_ref: JsonObject | None = ...
    receipt: JsonObject | None = ...
    file_name: str | None = ...
    size_bytes: int | None = ...

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
    error_code: Incomplete
    max_deferred_attempts: Incomplete
    deferred_backoff_base_seconds: Incomplete
    def __init__(self, message: str, *, error_code: str, max_deferred_attempts: int, deferred_backoff_base_seconds: int) -> None: ...

class ThumbnailBackendUnavailable(RuntimeError):
    error_code: Incomplete
    def __init__(self, message: str, *, error_code: str) -> None: ...

@dataclass(frozen=True)
class ClipArtifact:
    relative_path: str

@dataclass(frozen=True)
class DownloadSubmission:
    source_uri: str
    display_name: str
    torrent_bytes: bytes | None = ...
    source_kind: DownloadSourceKind | None = ...
    resource_key: str | None = ...

@dataclass(frozen=True)
class RemoteDownloadTask:
    remote_id: str
    name: str
    state: Literal['queued', 'downloading', 'completed', 'failed']
    progress: float
    completed_source_ref: JsonObject | None
    def __post_init__(self) -> None: ...

class ProviderOperationError(RuntimeError):
    provider_key: str
    operation: str
    code: Literal['invalid_config', 'authentication_failed', 'source_not_found', 'task_not_managed', 'source_blacklisted', 'unsupported', 'unavailable']
    safe_message: str
    retryable: bool
    def __init__(self, provider_key: str, operation: str, code: Literal['invalid_config', 'authentication_failed', 'source_not_found', 'task_not_managed', 'source_blacklisted', 'unsupported', 'unavailable'], safe_message: str, retryable: bool) -> None: ...

class DownloadProvider(Protocol):
    def submit(self, *, submission: DownloadSubmission) -> RemoteDownloadTask: ...
    def list_tasks(self) -> tuple[RemoteDownloadTask, ...]: ...
    def delete_task(self, *, remote_id: str, delete_files: bool) -> None: ...

class StorageProvider(Protocol):
    def browse(self, *, parent_ref: JsonObject | None, cursor: str | None, limit: int) -> BrowsePage: ...
    def scan_import_source(self, *, source_ref: JsonObject, progress_callback: ScanProgressCallback | None = None) -> Iterable[ImportFile]: ...
    def read_import_file(self, *, source: ImportFile) -> ImportFileContent: ...
    def delete_import_file(self, *, receipt: JsonObject) -> None: ...
    def stage_import_file(self, *, source: ImportFile, placement: ImportPlacement, source_disposition: Literal['keep', 'delete_after_commit', 'in_place'], operation_key: str) -> StagedMedia: ...
    def finalize_import(self, *, receipt: JsonObject) -> None: ...
    def abort_import(self, *, receipt: JsonObject) -> None: ...
    def delete_media(self, *, media: MediaHandle) -> None: ...
    def compute_file_hash(self, *, media: MediaHandle) -> str: ...
    async def handle_playback(self, *, media: MediaHandle, context: PlaybackContext) -> Response: ...
    def generate_thumbnails(self, *, media: MediaHandle, workspace: Path, progress_callback: Callable[[str], None] | None = None) -> ThumbnailGeneration: ...
    def create_clip(self, *, media: MediaHandle, start_offset_seconds: int, end_offset_seconds: int, workspace: Path) -> ClipArtifact: ...

class StorageMergedPlaybackProvider(Protocol):
    async def handle_merged_playback(self, *, medias: tuple[MediaHandle, ...], context: PlaybackContext) -> Response: ...

class StorageMergedPlaybackPreflightProvider(Protocol):
    def preflight_merged_playback(self, *, medias: tuple[MediaHandle, ...]) -> None: ...

class MediaProviderMergedPlaybackBundle(Protocol):
    merged_playback_format: MergedPlaybackFormat

class StorageImportSourceIdentityProvider(Protocol):
    def get_import_source_identity(self, *, source: ImportFile) -> str | None: ...

class StorageImportSourceValidationProvider(Protocol):
    def validate_import_source(self, *, source_ref: JsonObject, source_disposition: str) -> None: ...

class StorageMediaTransferSourceProvider(Protocol):
    def open_transfer_source(self, *, media: MediaHandle) -> AbstractContextManager[MediaTransferSource]: ...

class StorageMediaTransferSourceCleanupProvider(Protocol):
    def cleanup_transfer_source(self, *, media: MediaHandle, source: MediaTransferSource) -> None: ...

class StorageMediaTransferTargetProvider(Protocol):
    def stage_transfer(self, *, source: MediaTransferSource, placement: ImportPlacement, operation_key: str) -> StagedMediaTransfer: ...
    def finalize_transfer(self, *, receipt: JsonObject) -> None: ...
    def abort_transfer(self, *, receipt: JsonObject) -> None: ...

class StorageMediaRefScanner(Protocol):
    def scan_media_refs(self, *, source_ref: JsonObject) -> Iterable[JsonObject]: ...

class StorageManagedMediaRefScanner(Protocol):
    def scan_managed_media_ref_keys(self) -> set[str]: ...
    def managed_media_ref_key(self, *, media_ref: JsonObject) -> str: ...

class StorageCoverSourceProvider(Protocol):
    def open_cover_source(self, *, media: MediaHandle) -> AbstractContextManager[Any]: ...

class StorageDurationProbeProvider(Protocol):
    def probe_duration_seconds(self, *, media: MediaHandle) -> int: ...

class StorageResolutionProbeProvider(Protocol):
    def probe_resolution(self, *, media: MediaHandle) -> str | None: ...

class StorageVideoInfoProbeProvider(Protocol):
    def probe_video_info(self, *, media: MediaHandle) -> JsonObject | None: ...

@dataclass(frozen=True)
class StorageSpaceUsage:
    total_bytes: int | None = ...
    used_bytes: int | None = ...
    free_bytes: int | None = ...

class StorageSpaceProvider(Protocol):
    def get_space_usage(self) -> StorageSpaceUsage: ...

class DownloadComponent(Protocol):
    config_fields: tuple[ConfigField, ...]
    supported_source_kinds: frozenset[DownloadSourceKind]
    def prepare_client(self, *, submitted_config: JsonObject, library: LibraryHandle, previous: DownloadClientHandle | None) -> JsonObject: ...
    def test_client(self, *, submitted_config: JsonObject, library: LibraryHandle) -> ProviderDiagnosticReport: ...
    def build(self, *, client: DownloadClientHandle) -> DownloadProvider: ...

class MediaProviderBundle(Protocol):
    provider_key: str
    display_name: str
    display_name_i18n: dict[str, str] | None
    library_config_fields: tuple[ConfigField, ...]
    playback_deliveries: tuple[PlaybackDelivery, ...]
    downloads: DownloadComponent | None
    library_login: LibraryLoginSpec | None
    def prepare_library(self, *, submitted_config: JsonObject, previous: LibraryHandle | None) -> PreparedLibrary: ...
    def build_storage(self, *, library: LibraryHandle) -> StorageProvider: ...

def supports_media_transfer_source(provider: object) -> TypeGuard[StorageMediaTransferSourceProvider]: ...
def supports_media_transfer_source_cleanup(provider: object) -> TypeGuard[StorageMediaTransferSourceCleanupProvider]: ...
def supports_media_transfer_target(provider: object) -> TypeGuard[StorageMediaTransferTargetProvider]: ...
def supports_import_source_validation(provider: object) -> TypeGuard[StorageImportSourceValidationProvider]: ...
def supports_space_usage(provider: object) -> TypeGuard[StorageSpaceProvider]: ...

class ProviderUnavailableError(LookupError): ...

class MediaProviderRegistry:
    def __init__(self) -> None: ...
    def supports_scan_progress(self, provider_key: str) -> bool: ...
    def supports_in_place_import(self, provider_key: str) -> bool: ...
    def supports_torrent_bytes(self, provider_key: str) -> bool: ...
    def supported_source_kinds(self, provider_key: str) -> frozenset[DownloadSourceKind]: ...
    def require(self, provider_key: str) -> MediaProviderBundle: ...
    def list_bundles(self) -> tuple[MediaProviderBundle, ...]: ...
    def provider_keys_for_plugin(self, plugin_id: str) -> tuple[str, ...]: ...
    def replace(self, registrations: Iterable[Any]) -> set[str]: ...
    def storage_for(self, library: LibraryHandle) -> StorageProvider: ...
    def download_for(self, client: DownloadClientHandle) -> DownloadProvider: ...

MEDIA_PROVIDER_REGISTRY: Incomplete

def refresh_media_provider_registry(registrations: Iterable[Any]) -> set[str]: ...
