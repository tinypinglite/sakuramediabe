from datetime import datetime
from enum import Enum
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


class StatusImageSearchIndexSpaceSummary(SchemaModel):
    state: Literal["ready", "rebuild_required", "uninitialized", "unavailable"]
    indexed_space_id: str | None = None
    current_space_id: str | None = None
    is_rebuilding: bool = False


class StatusImageSearchResource(SchemaModel):
    enabled: bool = True
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


class StatusWatchTrendRange(str, Enum):
    """观看趋势时间范围；粒度随范围推导：7d/30d/90d 按天，1y/all 按月。"""

    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    LAST_90_DAYS = "90d"
    LAST_YEAR = "1y"
    ALL = "all"


class StatusWatchTrendGranularity(str, Enum):
    DAY = "day"
    MONTH = "month"


class StatusWatchTrendBucket(SchemaModel):
    # 天粒度 "YYYY-MM-DD"，月粒度 "YYYY-MM"；区间无观看记录时补零，保证时间轴连续。
    period: str
    count: int


class StatusWatchTrendResource(SchemaModel):
    """观看趋势。

    数据源是每个媒体只保留最后一次的 ``MediaProgress.last_watched_at``，所以语义是
    "最后观看时间落在各区间"的分布，不是完整观看历史，更早的记录已被覆盖。
    只统计 JAV 影片；非 JAV 视频不参与。分桶按运行时本地时区。
    """

    range: StatusWatchTrendRange
    granularity: StatusWatchTrendGranularity
    watched_movie_count: int
    buckets: list[StatusWatchTrendBucket]


class StatusDownloadTaskSummary(SchemaModel):
    """下载任务状态分布（当前任务快照，非历史累计）。六类互斥，合计恒等于 total。"""

    downloading: int = 0
    importing: int = 0
    imported: int = 0
    import_failed: int = 0
    skipped: int = 0
    download_failed: int = 0
    total: int = 0


class StatusMediaLibraryUsage(SchemaModel):
    library_id: int
    name: str
    provider_key: str
    # 与 /status 的 media_files 口径一致：含失效媒体，各库之和恒等于全局总数。
    file_count: int
    total_size_bytes: int
    # 存储端容量，来自 provider 可选能力 get_space_usage；null 表示不支持或查询失败。
    space_total_bytes: int | None = None
    space_used_bytes: int | None = None
    space_free_bytes: int | None = None


class StatusCollectionSummary(SchemaModel):
    count: int = 0
    item_count: int = 0


class StatusCollectionsSummary(SchemaModel):
    # playlists 不含系统「最近播放」列表，它不是用户创建的资产。
    playlists: StatusCollectionSummary
    video_collections: StatusCollectionSummary
    clip_collections: StatusCollectionSummary
    moment_collections: StatusCollectionSummary


class StatusInsightsResource(SchemaModel):
    download_tasks: StatusDownloadTaskSummary
    media_libraries: list[StatusMediaLibraryUsage]
    collections: StatusCollectionsSummary
