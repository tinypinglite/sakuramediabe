import peewee
from peewee import SQL

from src.model.base import BaseModel, JsonbField
from src.model.mixins import TimestampedMixin


class DownloadSelectionPolicy(TimestampedMixin, BaseModel):
    """单账号系统的全局选种策略，全库只允许 id=1 一行。"""

    rules = JsonbField(
        null=False,
        default=dict,
        constraints=[SQL("DEFAULT '{}'::jsonb")],
    )
    version = peewee.BigIntegerField(null=False, default=1)

    class Meta:
        table_name = "download_selection_policy"
        constraints = [
            SQL(
                "CONSTRAINT download_selection_policy_singleton "
                "CHECK (id = 1)"
            )
        ]
