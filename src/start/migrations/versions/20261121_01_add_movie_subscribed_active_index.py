"""Add a partial index for subscribed movie counts."""

name = "20261121_01_add_movie_subscribed_active_index"


def migrate(database) -> None:
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS movie_subscribed_active "
        "ON movie (id) WHERE is_subscribed AND NOT is_blacklisted"
    )
