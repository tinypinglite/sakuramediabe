from __future__ import annotations

import peewee
from playhouse.migrate import migrate as run_migration

from src.model import MovieSeries
from src.model.base import BaseModel
from src.start.migrations import SkipMigration


name = "20260424_01_extract_movie_series"


class _LegacyMovie(BaseModel):
    series_name = peewee.CharField(max_length=255, null=True)
    series = peewee.ForeignKeyField(MovieSeries, null=True, backref="legacy_movies")

    class Meta:
        table_name = "movie"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def _index_exists(database, *, table_name: str, index_name: str) -> bool:
    return any(index.name == index_name for index in database.get_indexes(table_name))


def _ensure_movie_series_table(database) -> None:
    # 迁移命令会先补齐当前模型表；旧库直跑 migration 时这里负责补建系列表。
    with database.bind_ctx([MovieSeries], bind_refs=False, bind_backrefs=False):
        database.create_tables([MovieSeries], safe=True)


def _ensure_series_column(database, migrator) -> None:
    if not _column_exists(database, table_name="movie", column_name="series_id"):
        series_field = peewee.ForeignKeyField(
            MovieSeries,
            field=MovieSeries.id,
            null=True,
            on_delete="SET NULL",
        )
        run_migration(migrator.add_column("movie", "series_id", series_field))
    if not _index_exists(database, table_name="movie", index_name="movie_series_id"):
        run_migration(migrator.add_index("movie", ("series_id",), False))


def _drop_legacy_series_name_index(database, migrator) -> None:
    if _index_exists(database, table_name="movie", index_name="movie_series_name"):
        # SQLite 删除列前要求先移除引用该列的旧索引，否则 DROP COLUMN 会失败。
        run_migration(migrator.drop_index("movie", "movie_series_name"))


def _get_or_create_series(name: str) -> MovieSeries:
    series, _ = MovieSeries.get_or_create(name=name)
    return series


def _backfill_series_ids(database) -> None:
    with database.bind_ctx([MovieSeries, _LegacyMovie], bind_refs=False, bind_backrefs=False):
        rows = list(_LegacyMovie.select(_LegacyMovie.id, _LegacyMovie.series_name))
        for row in rows:
            normalized_name = (row.series_name or "").strip()
            if not normalized_name:
                _LegacyMovie.update(series=None).where(_LegacyMovie.id == row.id).execute()
                continue
            series = _get_or_create_series(normalized_name)
            _LegacyMovie.update(series=series).where(_LegacyMovie.id == row.id).execute()


def migrate(database, migrator) -> None:
    if not database.table_exists("movie"):
        # 目标表尚未建出时不能误记迁移完成，留待后续建表后再执行。
        raise SkipMigration("movie table does not exist")

    _ensure_movie_series_table(database)
    _ensure_series_column(database, migrator)

    if not _column_exists(database, table_name="movie", column_name="series_name"):
        return

    _backfill_series_ids(database)
    _drop_legacy_series_name_index(database, migrator)
    run_migration(migrator.drop_column("movie", "series_name"))
