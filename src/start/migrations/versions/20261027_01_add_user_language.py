"""新增用户界面语言偏好。

NULL 表示首次登录尚未选择；应用层按 Accept-Language -> User.language -> zh-CN
解析。不回填存量用户，避免把默认中文误写成显式选择。
"""

name = "20261027_01_add_user_language"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR(16) NULL"
    )
