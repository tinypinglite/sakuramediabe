from __future__ import annotations

from src.model import Media, MediaPoint, MediaThumbnail, MomentRecommendation, Movie
from src.start.migrations import SkipMigration

name = "20260508_02_add_moment_recommendations"


def migrate(database, migrator) -> None:
    # 新表通过 Peewee 模型创建，字段定义统一交由 PostgreSQL 后端渲染。
    required_tables = {"movie", "media", "media_thumbnail", "media_point"}
    existing_tables = set(database.get_tables())
    if not required_tables.issubset(existing_tables):
        raise SkipMigration()
    with database.bind_ctx(
        [Movie, Media, MediaThumbnail, MediaPoint, MomentRecommendation],
        bind_refs=False,
        bind_backrefs=False,
    ):
        database.create_tables([MomentRecommendation], safe=True)
