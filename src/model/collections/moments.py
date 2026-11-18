import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin
from src.model.playback.media import MediaPoint


class MomentCollection(TimestampedMixin, BaseModel):
    """时刻合集：用户整理的一组有序媒体时刻。"""

    name = peewee.CharField(max_length=255, unique=True)
    description = peewee.TextField(default="")
    owner_plugin_id = peewee.CharField(max_length=64, null=True)
    plugin_key = peewee.CharField(max_length=128, null=True)

    class Meta:
        table_name = "moment_collection"
        indexes = (("owner_plugin_id", "plugin_key"), True),


class MomentCollectionItem(TimestampedMixin, BaseModel):
    """时刻合集成员；时刻删除后自动移出合集。"""

    collection = peewee.ForeignKeyField(
        MomentCollection, backref="items", on_delete="CASCADE"
    )
    point = peewee.ForeignKeyField(
        MediaPoint, backref="collection_items", on_delete="CASCADE"
    )
    position = peewee.IntegerField(index=True)

    class Meta:
        table_name = "moment_collection_item"
        indexes = ((("collection", "point"), True),)
