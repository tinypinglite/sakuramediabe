from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.common.runtime_time import utc_now_for_db
from src.model import IndexerDownloadClient
from src.start.migrations import SkipMigration

name = "20260714_07_indexer_multi_client_binding"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    # 索引器与下载器改多对多：建 indexer_download_client 中间表 → 用旧 FK 回填 → 删旧列。
    # 新装用户 initdb 已直接建出新 schema，各步幂等，命中已完成态时 skip。
    if not database.table_exists("indexer"):
        raise SkipMigration("indexer table does not exist")

    # 1) 建中间表（含 (indexer, download_client) 唯一索引），safe=True 幂等。
    database.create_tables([IndexerDownloadClient], safe=True)

    # 2) 回填：把旧的单 FK 绑定搬进中间表。仅在旧列还在时执行，天然幂等
    #    （删列是本迁移最后一步，中断重跑会重新进入这里，ON CONFLICT 防重复）。
    if _column_exists(database, table_name="indexer", column_name="download_client_id"):
        # 时间戳统一走 Python 侧 utc_now_for_db()（naive UTC），与全项目 mixins.py
        # 的默认值一致；不用数据库端 NOW()——那会随 Postgres 会话时区把带偏移的
        # 时间转换后再写入 timestamp without time zone，导致与应用侧数据不同步。
        now = utc_now_for_db()
        database.execute_sql(
            """
            INSERT INTO indexer_download_client
                (indexer_id, download_client_id, created_at, updated_at)
            SELECT id, download_client_id, %s, %s
            FROM indexer
            WHERE download_client_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """,
            (now, now),
        )

        # 3) 删旧 FK 列（peewee 会自动带走关联索引）。
        run_migration(migrator.drop_column("indexer", "download_client_id"))
