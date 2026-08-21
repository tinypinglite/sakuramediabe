"""把合集人工覆盖合并到影片字段 owner 映射。"""

from __future__ import annotations

name = "20260823_01_unify_movie_collection_owner"


def migrate(database) -> None:
    """保留历史人工标记，并删除重复的 override 列。"""
    has_legacy_column = database.execute_sql(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'movie'
          AND column_name = 'is_collection_overridden'
        """
    ).fetchone()
    if has_legacy_column is None:
        # 全新库直接按当前模型建表，不存在需要合并的历史列。
        return

    database.execute_sql(
        """
        UPDATE movie
        SET field_owners = COALESCE(field_owners, '{}'::jsonb)
            || '{"is_collection": "host:manual"}'::jsonb
        WHERE is_collection_overridden = TRUE
        """
    )
    database.execute_sql(
        "ALTER TABLE movie DROP COLUMN is_collection_overridden"
    )
