import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin


class MediaLibrary(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    root_path = peewee.CharField(max_length=1024, unique=True)

    class Meta:
        table_name = "media_library"
