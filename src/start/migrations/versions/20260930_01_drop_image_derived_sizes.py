name = "20260930_01_drop_image_derived_sizes"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE image "
        "DROP COLUMN IF EXISTS small, "
        "DROP COLUMN IF EXISTS medium, "
        "DROP COLUMN IF EXISTS large"
    )
