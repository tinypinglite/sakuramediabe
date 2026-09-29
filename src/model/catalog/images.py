import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin


class Image(TimestampedMixin, BaseModel):
    origin = peewee.CharField(unique=True, max_length=255, help_text="原图路径")

    class Meta:
        table_name = "image"
