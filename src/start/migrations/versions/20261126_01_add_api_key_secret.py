"""Store a recoverable API key value for agent connection reuse."""

name = "20261126_01_add_api_key_secret"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_value VARCHAR(128) NULL"
    )
