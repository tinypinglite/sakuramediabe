import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin


class Image(TimestampedMixin, BaseModel):
    origin = peewee.CharField(unique=True, max_length=255, help_text="原图路径")
    small = peewee.CharField(max_length=255, null=False, help_text="缩略图路径")
    medium = peewee.CharField(max_length=255, null=False, help_text="中图路径")
    large = peewee.CharField(max_length=255, null=False, help_text="大图路径")

    class Meta:
        table_name = "image"
