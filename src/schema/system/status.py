from datetime import datetime
from typing import Literal

from pydantic import Field

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
    # pending 为当前可领取量；retry_wait 未到退避时间；terminal 须人工点名重试。
    pending_media: int
    retry_wait_media: int
    terminal_failed_media: int
    total: int


class StatusResource(SchemaModel):
    backend_version: str
    actors: StatusActorSummary
    movies: StatusMovieSummary
    media_files: StatusMediaFileSummary
    media_libraries: StatusMediaLibrarySummary
    thumbnails: StatusThumbnailSummary


class StatusEmbeddingServiceSummary(SchemaModel):
    healthy: bool
    endpoint: str | None = None
    space_id: str | None = None
    dimension: int | None = None
    modalities: list[str] = Field(default_factory=list)
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


class StatusImageSearchIndexSpaceSummary(SchemaModel):
    state: Literal["ready", "rebuild_required", "uninitialized", "unavailable"]
    indexed_space_id: str | None = None
    current_space_id: str | None = None
    is_rebuilding: bool = False


class StatusImageSearchResource(SchemaModel):
    healthy: bool
    checked_at: datetime
    embedding_service: StatusEmbeddingServiceSummary
    image_search_vector_store: StatusImageSearchVectorStoreSummary
    indexing: StatusImageSearchIndexingSummary
    index_space: StatusImageSearchIndexSpaceSummary


class ImageSearchResetResource(SchemaModel):
    task_run_id: int


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
