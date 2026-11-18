"""为下载任务增加远端可见时间，供幽灵任务保守清理使用。

单次远端快照缺席不足以判定任务已被删除：分页漂移、任务注册可见性延迟、与提交
落库的时序竞态都会造成假阴性。新增 remote_seen_at 记录最近一次在快照中出现的
时间；存量行回填为迁移时刻，让它们在部署后先获得一个完整确认窗，避免第一轮同步
把分页漏检的旧任务立即当作幽灵删除。
"""

name = "20261007_02_add_download_task_remote_seen_at"


def migrate(database) -> None:
    tables = set(database.get_tables())
    if "download_task" not in tables:
        return
    columns = {column.name for column in database.get_columns("download_task")}
    if "remote_seen_at" not in columns:
        database.execute_sql(
            'ALTER TABLE "download_task" ADD COLUMN "remote_seen_at" TIMESTAMP NULL'
        )
    # 存量行没有"最近可见"历史：回填为当前时刻（新库无行，自然为空操作），
    # 避免部署后的第一轮同步把分页漏检的旧任务立即当作幽灵删除。
    from src.common.runtime_time import utc_now_for_db

    database.execute_sql(
        'UPDATE "download_task" SET "remote_seen_at" = %s WHERE "remote_seen_at" IS NULL',
        (utc_now_for_db(),),
    )
