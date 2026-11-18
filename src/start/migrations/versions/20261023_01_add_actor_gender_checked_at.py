"""新增演员性别回填状态。

gender_checked_at 记录已评估时间；gender_next_check_at 记录请求失败后的退避时间。
新库由 create_tables 直接建列。
"""

name = "20261023_01_add_actor_gender_checked_at"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE actor ADD COLUMN IF NOT EXISTS gender_checked_at TIMESTAMP NULL"
    )
    database.execute_sql(
        "ALTER TABLE actor ADD COLUMN IF NOT EXISTS gender_next_check_at TIMESTAMP NULL"
    )
