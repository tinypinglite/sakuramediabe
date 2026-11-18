from _typeshed import Incomplete
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

__all__ = ['ACTOR_SNAPSHOT_FIELDS', 'MOVIE_SNAPSHOT_FIELDS', 'ActorPage', 'ActorSnapshot', 'ImageDownloadError', 'MoviePage', 'MovieQueryFilters', 'MovieSnapshot', 'PluginBrowseEntry', 'PluginBrowsePage', 'PluginCollection', 'PluginDownloadCandidate', 'PluginDownloadResult', 'PluginDownloadTarget', 'PluginImportBatch', 'PluginImportStatus', 'PluginLibrary', 'PluginMediaPresence', 'PluginMediaSnapshot', 'PluginNotification', 'PluginSubscription', 'PluginSubscriptionPage', 'PluginSubscriptionStatusCounts', 'PluginVideoPage', 'PluginVideoSnapshot', 'SubtitleAsset', 'SubtitleContent', 'SubtitleImportResult', 'SubtitleImportStatus', 'SubtitleReadError', 'TagSnapshot']

MOVIE_SNAPSHOT_FIELDS: tuple[str, ...]
ACTOR_SNAPSHOT_FIELDS: tuple[str, ...]

@dataclass(frozen=True)
class ActorSnapshot:
    actor_id: int
    revision: int
    values: Mapping[str, Any]
    owners: Mapping[str, str]

@dataclass(frozen=True)
class ActorPage:
    items: tuple[ActorSnapshot, ...]
    next_cursor: int | None

@dataclass(frozen=True)
class TagSnapshot:
    tag_id: int
    name: str

@dataclass(frozen=True)
class MovieSnapshot:
    movie_id: int
    revision: int
    values: Mapping[str, Any]
    owners: Mapping[str, str]
    actors: tuple[ActorSnapshot, ...] = ...
    tags: tuple[TagSnapshot, ...] = ...

@dataclass(frozen=True)
class MoviePage:
    items: tuple[MovieSnapshot, ...]
    next_cursor: int | None

@dataclass(frozen=True)
class MovieQueryFilters:
    search: str | None = ...
    actor_id: int | None = ...
    tag_ids: tuple[int, ...] = ...
    tag_match: str = ...
    year: int | None = ...
    subscribed: bool | None = ...
    playable: bool | None = ...
    status: str = ...
    collection_type: str = ...
    series_id: int | None = ...
    director_name: str | None = ...
    maker_name: str | None = ...
    number_source: str = ...
    heat_min: int | None = ...
    heat_max: int | None = ...
    blacklisted: bool = ...

@dataclass(frozen=True)
class PluginSubscription:
    movie_id: int
    movie_number: str
    title: str
    status: str
    subscribed_at: datetime | None
    is_fresh: bool
    attempt_count: int
    attempt_limit: int
    last_searched_at: datetime | None
    last_error: str | None
    import_status: str | None
    dead_download_task_count: int
    media_count: int

@dataclass(frozen=True)
class PluginSubscriptionPage:
    items: tuple[PluginSubscription, ...]
    page: int
    page_size: int
    total: int

@dataclass(frozen=True)
class PluginSubscriptionStatusCounts:
    counts: Mapping[str, int]

@dataclass(frozen=True)
class PluginNotification:
    notification_id: int
    category: str
    title: str
    content: str
    event_type: str | None
    dedupe_key: str | None
    resource_type: str | None
    resource_id: int | None
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True)
class PluginCollection:
    collection_type: str
    collection_id: int
    key: str
    name: str
    description: str
    member_count: int

@dataclass(frozen=True)
class PluginMediaSnapshot:
    media_id: int
    movie_id: int
    movie_number: str
    library_id: int
    library_name: str
    provider_key: str
    file_name: str
    resolution: str | None
    file_size_bytes: int
    duration_seconds: int
    valid: bool
    video_info: Mapping[str, Any] | None = ...

@dataclass(frozen=True)
class PluginMediaPresence:
    has_any: bool
    has_playable: bool
    items: tuple[PluginMediaSnapshot, ...]

@dataclass(frozen=True)
class PluginDownloadTarget:
    download_client_id: int
    download_client_name: str
    library_id: int
    library_name: str
    provider_key: str

@dataclass(frozen=True)
class PluginDownloadCandidate:
    source_uri: str
    indexer_name: str
    indexer_kind: str
    download_client_id: int
    download_client_name: str
    library_id: int
    library_name: str
    provider_key: str
    movie_number: str
    title: str
    size_bytes: int
    seeders: int

@dataclass(frozen=True)
class PluginDownloadResult:
    task_id: int
    created: bool

@dataclass(frozen=True)
class PluginLibrary:
    library_id: int
    name: str
    provider_key: str

@dataclass(frozen=True)
class PluginBrowseEntry:
    source_ref: Mapping[str, Any]
    name: str
    entry_type: str
    size_bytes: int | None
    modified_at: datetime | None
    is_video: bool

@dataclass(frozen=True)
class PluginBrowsePage:
    entries: tuple[PluginBrowseEntry, ...]
    next_cursor: str | None

@dataclass(frozen=True)
class PluginImportBatch:
    task_run_id: int
    task_key: str
    state: str

@dataclass(frozen=True)
class PluginImportStatus:
    task_run_id: int
    state: str
    imported_count: int
    skipped_count: int
    failed_count: int
    created_video_ids: tuple[int, ...]
    movie_ids: tuple[int, ...]
    error_message: str | None

@dataclass(frozen=True)
class PluginVideoSnapshot:
    video_id: int
    title: str
    summary: str
    release_date: datetime | None
    media_count: int
    has_playable: bool
    duration_seconds: int
    file_size_bytes: int
    resolution: str | None
    file_name: str | None
    collection_ids: tuple[int, ...]
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True)
class PluginVideoPage:
    items: tuple[PluginVideoSnapshot, ...]
    next_cursor: int | None

class SubtitleImportStatus(str, Enum):
    IMPORTED = 'imported'
    DUPLICATE = 'duplicate'
    MOVIE_NOT_FOUND = 'movie_not_found'
    INVALID_FORMAT = 'invalid_format'

class SubtitleImportResult(BaseModel):
    status: SubtitleImportStatus
    subtitle_id: int | None
    reason: str | None

@dataclass(frozen=True)
class SubtitleAsset:
    subtitle_id: int
    file_name: str
    format: str
    size_bytes: int
    created_at: datetime

@dataclass(frozen=True)
class SubtitleContent:
    subtitle_id: int
    content: bytes
    sha256: str

class SubtitleReadError(ValueError):
    code: Incomplete
    def __init__(self, code: str, message: str) -> None: ...

class ImageDownloadError(Exception): ...
