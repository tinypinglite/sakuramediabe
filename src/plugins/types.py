"""插件可见的公开类型层。

插件从这里导入宿主数据类型，禁止直接 import ``src.model`` /
``src.service`` / ``src.metadata._providers``（由安装/加载时的白名单扫描强制）。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.metadata._providers.models import (
    JavdbMovieActor,
    JavdbMovieDetail,
    JavdbMovieTag,
)
from src.schema.catalog.subtitles import (
    SubtitleImportResult,
    SubtitleImportStatus,
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
)


@dataclass(frozen=True)
class MovieSnapshot:
    """影片不可变快照（v2-lite）：插件读取/导入的出口，绝不暴露内部 ORM 对象。

    - ``values``：MOVIE_SNAPSHOT_FIELDS 固定只读集合的快照值；
    - ``owners``：字段 -> owner 的接管映射（缺键代表自动宿主管理，``host:manual`` 代表人工）；
    - ``revision``：受保护字段版本，``patch`` 的乐观并发依据。
    """

    movie_id: int
    revision: int
    values: Mapping[str, Any]
    owners: Mapping[str, str]


@dataclass(frozen=True)
class MoviePage:
    """按影片内部 id 游标返回的一页影片快照。"""

    items: tuple[MovieSnapshot, ...]
    next_cursor: int | None


__all__ = [
    "MOVIE_SNAPSHOT_FIELDS",
    "ImageDownloadError",
    "JavdbMovieActor",
    "JavdbMovieDetail",
    "JavdbMovieTag",
    "MoviePage",
    "MovieSnapshot",
    "SubtitleImportResult",
    "SubtitleImportStatus",
]
