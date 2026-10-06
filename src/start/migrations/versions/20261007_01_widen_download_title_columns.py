"""放宽下载提交标题与下载任务名的长度为 TEXT。

索引器返回的种子标题没有长度上限，历史上按 varchar(255) 入库会在提交阶段触发
DataError；115 离线任务同步回来的原始名称同理。这里只改仍是旧类型的列，兼容
用户已自行改成 text 的库。
"""

name = "20261007_01_widen_download_title_columns"


def migrate(database) -> None:
    tables = set(database.get_tables())
    for table_name, column_name in (
        ("download_submission_record", "title"),
        ("download_task", "name"),
    ):
        if table_name not in tables:
            continue
        columns = {column.name: column for column in database.get_columns(table_name)}
        column = columns.get(column_name)
        # 表/列缺失，或用户已自行改成 text，都不需要变更。
        if column is None or column.data_type == "text":
            continue
        database.execute_sql(
            f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" TYPE TEXT'
        )
