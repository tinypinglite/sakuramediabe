"""为下载提交记录补查询索引。

同步每分钟按 (client_id, remote_id / info_hash) 查任务重认领记录，并按 updated_at
扫孤儿下载器；该表只增不减，缺索引会退化为全表扫。索引名与模型声明一致：存量库
由本迁移补齐，新库由 create_tables 直接建出。
"""

name = "20261007_03_add_download_submission_record_indexes"


def migrate(database) -> None:
    if "download_submission_record" not in set(database.get_tables()):
        return
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS downloadsubmissionrecord_client_id_remote_id "
        "ON download_submission_record (client_id, remote_id)"
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS downloadsubmissionrecord_client_id_info_hash "
        "ON download_submission_record (client_id, info_hash)"
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS downloadsubmissionrecord_updated_at "
        "ON download_submission_record (updated_at)"
    )
