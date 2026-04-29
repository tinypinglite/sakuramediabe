from __future__ import annotations

import peewee
from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration


name = "20260421_01_add_movie_title_zh"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    if not database.table_exists("movie"):
        # 目标表尚未建出时不能误记迁移完成，留待后续建表后再判定是否需要补列。
        raise SkipMigration("movie table does not exist")

    if _column_exists(database, table_name="movie", column_name="title_zh"):
        return

    # 当前受支持的旧版 movie schema 只缺少 title_zh，这里仅负责补这个字段。
    # 标题译文字段保持空字符串默认值，避免旧数据迁移后出现空值分支。
    run_migration(
        migrator.add_column(
            "movie",
            "title_zh",
            peewee.TextField(default=""),
        )
    )
