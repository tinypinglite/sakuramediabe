from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.model import MediaRapidUploadItem
from src.start.migrations import SkipMigration

name = "20260720_01_add_media_rapid_upload_item_failure_reason"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    # 新增 failure_reason 用来把"115 未命中"这种确定性失败从其它可重试失败里分出来，
    # 供 media 列表接口告诉前端"下次是否值得重试"。老 item 保持 null；能靠 error_message
    # 精确识别的两种（not_hit / file_changed）顺手回填，其余归为 null（前端按可重试处理）。
    if not database.table_exists("media_rapid_upload_item"):
        raise SkipMigration("media_rapid_upload_item table does not exist")

    if not _column_exists(
        database,
        table_name="media_rapid_upload_item",
        column_name="failure_reason",
    ):
        run_migration(
            migrator.add_column(
                "media_rapid_upload_item",
                "failure_reason",
                MediaRapidUploadItem._meta.fields["failure_reason"],
            )
        )

    # 老写入侧异常文案固定为 f"rapid upload status={value}"，精确匹配两种可识别的原因。
    # 老仅 error_message 里能提取信息，file_changed 之外的其它失败都留 null。
    database.execute_sql(
        "UPDATE media_rapid_upload_item "
        "SET failure_reason = %s "
        "WHERE state = %s AND failure_reason IS NULL AND error_message = %s",
        ("not_hit", "failed", "rapid upload status=not_hit"),
    )
    database.execute_sql(
        "UPDATE media_rapid_upload_item "
        "SET failure_reason = %s "
        "WHERE state = %s AND failure_reason IS NULL AND error_message = %s",
        ("file_changed", "failed", "rapid upload status=file_changed"),
    )
