"""允许用户自建没有 JavDB 身份的女优。

本地女优以 ``javdb_id IS NULL`` 表示；PostgreSQL 唯一约束允许多个 NULL，
因此现有唯一索引无需调整。新库由当前 Peewee 模型直接建出可空列。
"""

name = "20261025_01_allow_local_actor_without_javdb_id"


def migrate(database) -> None:
    if "javdb_id" not in {column.name for column in database.get_columns("actor")}:
        return
    database.execute_sql("ALTER TABLE actor ALTER COLUMN javdb_id DROP NOT NULL")
