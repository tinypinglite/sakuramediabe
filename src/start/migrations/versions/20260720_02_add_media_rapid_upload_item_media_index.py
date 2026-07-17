from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration

name = "20260720_02_add_media_rapid_upload_item_media_index"


def _index_has_columns(index, columns: tuple[str, ...]) -> bool:
    return tuple(getattr(index, "columns", []) or []) == columns


def migrate(database, migrator) -> None:
    # 给 media_id 加单列索引：list_media 分页调 get_latest_status_by_media 走 DISTINCT ON
    # 时能命中该索引；否则跨 batch 的 media_ids IN(...) 查询无法利用现有的
    # (batch, media) UNIQUE / (batch, state) 复合索引（前缀都是 batch）。
    if not database.table_exists("media_rapid_upload_item"):
        raise SkipMigration("media_rapid_upload_item table does not exist")
    # 幂等：initdb 已用 index=True 建过等价索引时不重复建。
    if any(
        _index_has_columns(index, ("media_id",))
        for index in database.get_indexes("media_rapid_upload_item")
    ):
        return
    run_migration(
        migrator.add_index("media_rapid_upload_item", ("media_id",), False)
    )
