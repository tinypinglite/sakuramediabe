from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.model import DownloadClient, DownloadTask
from src.start.migrations import SkipMigration

name = "20260714_06_add_download_client_kind_and_task_target_ref"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    # download_client 引入 kind（下载入口种类），qb 专属连接字段放开为可空；
    # download_task 加 target_ref（cloud115 落地目录 cid 等结构化定位符）。
    # 新装用户 initdb 已直接建出新 schema，各步幂等，命中已完成态时 skip。
    if not database.table_exists("download_client"):
        raise SkipMigration("download_client table does not exist")

    # 1) kind 列：NOT NULL + default='qbittorrent'，存量行由方言引擎一次性回填为 qb。
    if not _column_exists(database, table_name="download_client", column_name="kind"):
        run_migration(
            migrator.add_column(
                "download_client",
                "kind",
                DownloadClient._meta.fields["kind"],
            )
        )

    # 2) qb 专属字段放开 NOT NULL（幂等：对已可空列再 drop_not_null 无副作用）。
    for column_name in ("base_url", "username", "password", "client_save_path", "local_root_path"):
        run_migration(migrator.drop_not_null("download_client", column_name))

    # 3) download_task.target_ref：可空 JSON，存量 qb 任务保持 NULL。
    if not _column_exists(database, table_name="download_task", column_name="target_ref"):
        run_migration(
            migrator.add_column(
                "download_task",
                "target_ref",
                DownloadTask._meta.fields["target_ref"],
            )
        )
