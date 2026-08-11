"""插件可见的公开类型层。

插件从这里导入宿主数据类型，禁止直接 import ``src.model`` /
``src.service`` / ``src.metadata._providers``（由安装/加载时的白名单扫描强制）。
"""

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

__all__ = [
    "ImageDownloadError",
    "JavdbMovieActor",
    "JavdbMovieDetail",
    "JavdbMovieTag",
    "SubtitleImportResult",
    "SubtitleImportStatus",
]
