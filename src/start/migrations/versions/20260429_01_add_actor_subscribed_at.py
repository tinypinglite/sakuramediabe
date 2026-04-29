from __future__ import annotations

import peewee
from playhouse.migrate import migrate as run_migration

from src.model.base import BaseModel
from src.start.migrations import SkipMigration


name = "20260429_01_add_actor_subscribed_at"


class _LegacyActor(BaseModel):
    is_subscribed = peewee.BooleanField(default=False)
    created_at = peewee.DateTimeField()
    subscribed_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "actor"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def _index_has_columns(index, columns: tuple[str, ...]) -> bool:
    return tuple(getattr(index, "columns", []) or []) == columns


def _ensure_subscribed_at_index(database, migrator) -> None:
    if any(_index_has_columns(index, ("subscribed_at",)) for index in database.get_indexes("actor")):
        return
    run_migration(migrator.add_index("actor", ("subscribed_at",), False))


def _backfill_legacy_subscribed_at(database) -> None:
    # 旧库没有真实订阅发生时间，按迁移约定用演员记录创建时间作为历史订阅时间。
    with database.bind_ctx([_LegacyActor], bind_refs=False, bind_backrefs=False):
        query = _LegacyActor.update(subscribed_at=_LegacyActor.created_at).where(
            _LegacyActor.is_subscribed == True,
            _LegacyActor.subscribed_at.is_null(),
        )
        query.execute()


def migrate(database, migrator) -> None:
    if not database.table_exists("actor"):
        # 目标表尚未建出时不能误记迁移完成，留待后续建表后再判定是否需要补列。
        raise SkipMigration("actor table does not exist")

    if not _column_exists(database, table_name="actor", column_name="subscribed_at"):
        run_migration(
            migrator.add_column(
                "actor",
                "subscribed_at",
                peewee.DateTimeField(null=True),
            )
        )

    _ensure_subscribed_at_index(database, migrator)
    _backfill_legacy_subscribed_at(database)
