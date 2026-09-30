name = "20261001_01_add_image_origin_pattern_index"


def migrate(database) -> None:
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS image_origin_pattern "
        "ON image (origin text_pattern_ops)"
    )
