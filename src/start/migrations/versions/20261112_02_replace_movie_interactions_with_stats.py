"""Replace scalar movie interactions with a JSON snapshot and plugin-owned heat."""

from __future__ import annotations

name = "20261112_02_replace_movie_interactions_with_stats"

_LEGACY_COLUMNS = (
    "score",
    "score_number",
    "watched_count",
    "want_watch_count",
    "comment_count",
)

_LEGACY_INDEXES = (
    "movie_score",
    "movie_score_sort",
    "movie_score_number_sort",
    "movie_watched_count_sort",
    "movie_want_watch_count_sort",
    "movie_comment_count_sort",
)


def migrate(database) -> None:
    database.execute_sql(
        """
        ALTER TABLE movie
            ADD COLUMN IF NOT EXISTS interaction_stats JSONB NULL,
            ADD COLUMN IF NOT EXISTS stats_source VARCHAR(64) NULL,
            ADD COLUMN IF NOT EXISTS stats_updated_at TIMESTAMP NULL,
            ADD COLUMN IF NOT EXISTS heat_version VARCHAR(128) NULL
        """
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS movie_stats_updated_at "
        "ON movie (stats_updated_at)"
    )

    columns = {column.name for column in database.get_columns("movie")}
    if not set(_LEGACY_COLUMNS).issubset(columns):
        return

    database.execute_sql(
        """
        UPDATE movie
        SET interaction_stats = jsonb_build_object(
                'score', score,
                'score_number', score_number,
                'watched_count', watched_count,
                'want_watch_count', want_watch_count,
                'comment_count', comment_count
            ),
            stats_source = 'legacy:host',
            heat_version = 'host:v6'
        WHERE interaction_stats IS NULL
          AND (
              score <> 0
              OR score_number <> 0
              OR watched_count <> 0
              OR want_watch_count <> 0
              OR comment_count <> 0
              OR heat <> 0
          )
        """
    )
    for index_name in _LEGACY_INDEXES:
        database.execute_sql(f"DROP INDEX IF EXISTS {index_name}")
    database.execute_sql(
        """
        ALTER TABLE movie
            DROP COLUMN IF EXISTS score,
            DROP COLUMN IF EXISTS score_number,
            DROP COLUMN IF EXISTS watched_count,
            DROP COLUMN IF EXISTS want_watch_count,
            DROP COLUMN IF EXISTS comment_count
        """
    )
