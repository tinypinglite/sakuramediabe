from datetime import datetime

from pydantic import Field

from src.lib.cloud115 import Cloud115CookieStatus
from src.schema.common.base import SchemaModel


class StatusActorSummary(SchemaModel):
    female_total: int
    female_subscribed: int


class StatusMovieSummary(SchemaModel):
    total: int
    subscribed: int
    playable: int


class StatusMediaFileSummary(SchemaModel):
    total: int
    total_size_bytes: int


class StatusMediaLibrarySummary(SchemaModel):
    total: int


class StatusThumbnailSummary(SchemaModel):
    # 待生成缩略图的媒体文件数量，以及已生成的缩略图文件总数。
    pending_media: int
    total: int


class StatusResource(SchemaModel):
    backend_version: str
    actors: StatusActorSummary
    movies: StatusMovieSummary
    media_files: StatusMediaFileSummary
    media_libraries: StatusMediaLibrarySummary
    thumbnails: StatusThumbnailSummary


class StatusCloud115LibraryCookieResource(SchemaModel):
    library_id: int
    name: str
    cookie_status: Cloud115CookieStatus


class StatusCloud115CookieSummary(SchemaModel):
    total: int
    alive: int
    expired: int
    unavailable: int


class StatusCloud115CookiesResource(SchemaModel):
    checked_at: datetime
    summary: StatusCloud115CookieSummary
    libraries: list[StatusCloud115LibraryCookieResource]


class StatusJoyTagSummary(SchemaModel):
    healthy: bool
    endpoint: str | None = None
    backend: str | None = None
    execution_provider: str | None = None
    used_device: str | None = None
    available_devices: list[str] = Field(default_factory=list)
    device_full_name: str | None = None
    prefer_gpu: bool | None = None
    model_dir: str | None = None
    model_file: str | None = None
    model_name: str | None = None
    vector_size: int | None = None
    image_size: int | None = None
    probe_latency_ms: int | None = None
    error: str | None = None


class StatusImageSearchVectorStoreSummary(SchemaModel):
    healthy: bool
    url: str
    collection_name: str
    exists: bool
    points_count: int | None = None
    vector_size: int | None = None
    vector_dtype: str | None = None
    collection_status: str | None = None
    error: str | None = None


class StatusImageSearchIndexingSummary(SchemaModel):
    pending_thumbnails: int
    failed_thumbnails: int
    success_thumbnails: int


class StatusImageSearchResource(SchemaModel):
    healthy: bool
    checked_at: datetime
    joytag: StatusJoyTagSummary
    image_search_vector_store: StatusImageSearchVectorStoreSummary
    indexing: StatusImageSearchIndexingSummary


class StatusMetadataProviderTestError(SchemaModel):
    type: str
    message: str
    method: str | None = None
    url: str | None = None
    resource: str | None = None
    lookup_value: str | None = None


class StatusMetadataProviderTestResource(SchemaModel):
    healthy: bool
    checked_at: datetime
    provider: str
    movie_number: str
    elapsed_ms: int
    error: StatusMetadataProviderTestError | None = None
    javdb_id: str | None = None
    title: str | None = None
    actors_count: int | None = None
    tags_count: int | None = None
