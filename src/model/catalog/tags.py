import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin


class Tag(TimestampedMixin, BaseModel):
    name = peewee.CharField(
        verbose_name="标签名称",
        unique=True,
        index=True,
        max_length=255,
    )

    class Meta:
        table_name = "tag"
