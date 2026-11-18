import peewee

from src.model.base import BaseModel, JsonbField
from src.model.mixins import TimestampedMixin
from src.model.playback.media import Media


class MediaTag(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=64, unique=True)
    include_terms = JsonbField(default=list)
    exclude_terms = JsonbField(default=list)
    enabled = peewee.BooleanField(default=True)

    class Meta:
        table_name = "media_tag"


class MediaMediaTag(BaseModel):
    media = peewee.ForeignKeyField(
        Media,
        backref="media_tag_links",
        on_delete="CASCADE",
        column_name="media_id",
    )
    tag = peewee.ForeignKeyField(
        MediaTag,
        backref="media_links",
        on_delete="CASCADE",
        column_name="tag_id",
    )

    class Meta:
        table_name = "media_media_tag"
        indexes = (
            (("media", "tag"), True),
            (("tag", "media"), False),
        )
