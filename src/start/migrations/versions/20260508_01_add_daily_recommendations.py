from __future__ import annotations

from src.model import DailyRecommendationItem, Movie
from src.start.migrations import SkipMigration


name = "20260508_01_add_daily_recommendations"


def migrate(database, migrator) -> None:
    if not database.table_exists("movie"):
        # 旧库还没建出影片表时不能误记迁移完成，留待建表后再次执行。
        raise SkipMigration("movie table does not exist")

    with database.bind_ctx([Movie, DailyRecommendationItem], bind_refs=False, bind_backrefs=False):
        database.create_tables([DailyRecommendationItem], safe=True)
