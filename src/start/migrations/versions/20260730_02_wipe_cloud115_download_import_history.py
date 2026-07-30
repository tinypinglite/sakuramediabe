from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260730_02_wipe_cloud115_download_import_history"


def migrate(database, migrator) -> None:
    """任务架构切换：清空导入作业台账与 cloud115 下载任务台账。

    只删 cloud115 客户端的 DownloadTask：cloud115 对账（Cloud115OfflineSyncService）
    从本地行出发去查远端，删掉不会复活；而 qBittorrent 的 DownloadSyncService.sync_client
    是 get_or_create，只要种子还在 qB 里就会重建，且 import_status 复位成 pending 触发
    重新导入，因此 qB 的行必须原样保留。

    用户可见后果（已确认接受）：订阅页原本卡在「导入失败」的 25 部影片回到「待查」，
    由自动下载任务重新找资源、重新下载。其中 12 部的 Media 是手动删除的（连 115 上的
    文件一并删了），重下即符合预期；另 13 部是 115 风控导致的导入失败，本就该重试。
    """
    required_tables = {"import_job", "video_import_job", "download_task", "download_client"}
    missing_tables = sorted(
        table_name for table_name in required_tables if not database.table_exists(table_name)
    )
    if missing_tables:
        raise SkipMigration(f"required tables do not exist: {missing_tables}")

    # 先删作业再删任务：import_job.download_task_id 是 ON DELETE SET NULL，
    # 反过来会先跑一轮无谓的置空 UPDATE。
    database.execute_sql("DELETE FROM import_job")
    database.execute_sql("DELETE FROM video_import_job")
    database.execute_sql(
        "DELETE FROM download_task WHERE client_id IN"
        " (SELECT id FROM download_client WHERE kind = %s)",
        ("cloud115",),
    )
