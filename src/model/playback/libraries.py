import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.enums import MediaLibraryBackend
from src.model.mixins import TimestampedMixin


class MediaLibrary(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    backend = peewee.CharField(max_length=32, default=MediaLibraryBackend.LOCAL.value)
    backend_config = JsonTextField(default=dict)

    class Meta:
        table_name = "media_library"
