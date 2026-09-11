"""插件可见的公开类型层。

插件从这里导入宿主数据类型，禁止直接 import ``src.model`` /
``src.service`` / ``src.metadata._providers``（由安装/加载时的白名单扫描强制）。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.metadata._providers.models import (
    JavdbMovieActor,
    JavdbMovieDetail,
    JavdbMovieTag,
)
from src.schema.catalog.subtitles import (
    SubtitleAsset,
    SubtitleContent,
    SubtitleImportResult,
    SubtitleImportStatus,
    SubtitleReadError,
)
from src.service.catalog.catalog_import_service import ImageDownloadError

# 宿主固定的公开只读字段集合：插件通过 MovieSnapshot.values 能读到且只能读到
# 这些字段。新 Movie 列不会因默认行为意外暴露；写入白名单（受保护字段）与
# 本集合是两回事，由 v2-lite 字段主权机制另行管理。
MOVIE_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "movie_number",
    "title",
    "summary",
    "release_date",
    "duration_minutes",
    "score",
    "score_number",
    "watched_count",
    "want_watch_count",
    "comment_count",
    "maker_name",
    "director_name",
    "series_name",
    "is_collection",
    "is_subscribed",
    "is_blacklisted",
)

ACTOR_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "javdb_id", "javdb_type", "name", "alias_name", "gender", "is_subscribed",
    "birthday", "height_cm", "bust_cm", "waist_cm", "hips_cm", "cup",
    "birthplace", "blood_type",
)


@dataclass(frozen=True)
class ActorSnapshot:
    """演员身份和资料快照；revision 只覆盖资料及字段归属。"""

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
    """影片关联标签的只读身份和名称。"""

    tag_id: int
    name: str


@dataclass(frozen=True)
class MovieSnapshot:
    """影片不可变快照（v2-lite）：插件读取/导入的出口，绝不暴露内部 ORM 对象。

    - ``values``：MOVIE_SNAPSHOT_FIELDS 固定只读集合的快照值；
    - ``owners``：字段 -> owner 的接管映射（缺键代表自动宿主管理，``host:manual`` 代表人工）；
    - ``revision``：受保护字段版本，``patch`` 的乐观并发依据。
    - ``actors`` / ``tags``：影片关联的只读快照元组，不由影片 revision 覆盖。
    """

    movie_id: int
    revision: int
    values: Mapping[str, Any]
    owners: Mapping[str, str]
    actors: tuple[ActorSnapshot, ...] = ()
    tags: tuple[TagSnapshot, ...] = ()


@dataclass(frozen=True)
class MoviePage:
    """按影片内部 id 游标返回的一页影片快照。"""

    items: tuple[MovieSnapshot, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class MovieQueryFilters:
    """影片游标查询的公开筛选参数；值域由宿主 facade 校验。"""

    search: str | None = None
    actor_id: int | None = None
    tag_ids: tuple[int, ...] = ()
    tag_match: str = "or"
    year: int | None = None
    subscribed: bool | None = None
    playable: bool | None = None
    status: str = "all"
    collection_type: str = "all"
    series_id: int | None = None
    director_name: str | None = None
    maker_name: str | None = None
    number_source: str = "all"
    heat_min: int | None = None
    heat_max: int | None = None
    blacklisted: bool = False


@dataclass(frozen=True)
class PluginSubscription:
    """插件可见的订阅影片状态快照。"""

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
    """插件创建的用户通知快照。"""

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
    """插件拥有的影片、时刻或片段合集。"""

    collection_type: str
    collection_id: int
    key: str
    name: str
    description: str
    member_count: int


@dataclass(frozen=True)
class PluginMediaSnapshot:
    """插件可见的单条 JAV 媒体快照；不暴露 provider 的 storage_ref。"""

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
    video_info: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PluginMediaPresence:
    """一部影片在指定媒体库中的媒体存在性与明细。"""

    has_any: bool
    has_playable: bool
    items: tuple[PluginMediaSnapshot, ...]


@dataclass(frozen=True)
class PluginDownloadTarget:
    """插件可使用的下载器及其媒体库目标快照。"""

    download_client_id: int
    download_client_name: str
    library_id: int
    library_name: str
    provider_key: str


@dataclass(frozen=True)
class PluginDownloadCandidate:
    """绑定到一个下载器/媒体库的宿主下载候选。"""

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
    """宿主下载提交结果。"""

    task_id: int
    created: bool


__all__ = [
    "ACTOR_SNAPSHOT_FIELDS",
    "MOVIE_SNAPSHOT_FIELDS",
    "ActorPage",
    "ActorSnapshot",
    "ImageDownloadError",
    "JavdbMovieActor",
    "JavdbMovieDetail",
    "JavdbMovieTag",
    "MoviePage",
    "MovieQueryFilters",
    "MovieSnapshot",
    "PluginCollection",
    "PluginDownloadCandidate",
    "PluginDownloadResult",
    "PluginDownloadTarget",
    "PluginMediaPresence",
    "PluginMediaSnapshot",
    "PluginNotification",
    "PluginSubscription",
    "PluginSubscriptionPage",
    "PluginSubscriptionStatusCounts",
    "SubtitleAsset",
    "SubtitleContent",
    "SubtitleImportResult",
    "SubtitleImportStatus",
    "SubtitleReadError",
    "TagSnapshot",
]
