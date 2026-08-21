"""为影片增加黑名单标记。"""

from __future__ import annotations

name = "20260823_03_add_movie_blacklist"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE movie ADD COLUMN IF NOT EXISTS is_blacklisted BOOLEAN NOT NULL DEFAULT FALSE"
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS movie_is_blacklisted ON movie (is_blacklisted)"
    )
    database.execute_sql(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'movie_subscription_blacklist_exclusive'
            ) THEN
                ALTER TABLE movie
                ADD CONSTRAINT movie_subscription_blacklist_exclusive
                CHECK (NOT (is_subscribed AND is_blacklisted));
            END IF;
        END $$;
        """
    )
