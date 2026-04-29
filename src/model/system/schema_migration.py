import peewee

from src.common.runtime_time import utc_now_for_db
from src.model.base import BaseModel


class SchemaMigration(BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True, verbose_name="迁移名称")
    applied_at = peewee.DateTimeField(default=utc_now_for_db, verbose_name="应用时间")

    class Meta:
        table_name = "schema_migration"
