import peewee

from src.common.runtime_time import utc_now_for_db
from src.model.base import BaseModel

VIEW_HISTORY_ENTITY_MOVIE = "movie"
VIEW_HISTORY_ENTITY_ACTOR = "actor"

# 最近浏览全局只保留最近 N 条（单账号系统，无需按用户分区）。
VIEW_HISTORY_MAX_ENTRIES = 200


class ViewHistory(BaseModel):
    """影片/女优的浏览记录：同一实体只保留最近一次浏览时间。"""

    entity_type = peewee.CharField(max_length=16)
    entity_id = peewee.IntegerField()
    viewed_at = peewee.DateTimeField(default=utc_now_for_db, index=True)

    class Meta:
        table_name = "view_history"
        indexes = ((("entity_type", "entity_id"), True),)
