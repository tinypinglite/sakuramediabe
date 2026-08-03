from __future__ import annotations

from src.model import (
    ClipCollection,
    ClipCollectionItem,
    Media,
    MediaClip,
    Movie,
)
from src.start.migrations import SkipMigration

name = "20260613_02_add_media_clip_tables"


def migrate(database, migrator) -> None:
    # 新表通过 Peewee 模型创建，字段定义统一交由 PostgreSQL 后端渲染。
    required_tables = {"movie", "media"}
    existing_tables = set(database.get_tables())
    if not required_tables.issubset(existing_tables):
        raise SkipMigration()
    with database.bind_ctx(
        [Movie, Media, MediaClip, ClipCollection, ClipCollectionItem],
        bind_refs=False,
        bind_backrefs=False,
    ):
        # 建表顺序：被引用方在前（MediaClip、ClipCollection），中间表 ClipCollectionItem 在后。
        database.create_tables(
            [MediaClip, ClipCollection, ClipCollectionItem],
            safe=True,
        )
