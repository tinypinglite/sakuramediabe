"""Add media source snapshot and clean release fields to submissions."""

name = "20261031_02_add_media_source_snapshot"


def migrate(database) -> None:
    for column_name, column_type in (
        ("release_title", "TEXT"),
        ("release_description", "TEXT"),
        ("source_indexer", "VARCHAR(255)"),
        ("info_hash", "VARCHAR(40)"),
        ("download_task_id", "INTEGER"),
    ):
        database.execute_sql(
            f"ALTER TABLE media ADD COLUMN IF NOT EXISTS {column_name} {column_type} NULL"
        )
    database.execute_sql(
        "ALTER TABLE download_submission_record "
        "ADD COLUMN IF NOT EXISTS release_title TEXT NULL"
    )
    database.execute_sql(
        "ALTER TABLE download_submission_record "
        "ADD COLUMN IF NOT EXISTS description TEXT NULL"
    )
