import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.mixins import TimestampedMixin


class MediaLibrary(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    provider_key = peewee.CharField(max_length=255, index=True)
    provider_config = JsonTextField(default=dict)
    account_key = peewee.CharField(max_length=255, null=True)

    class Meta:
        table_name = "media_library"
