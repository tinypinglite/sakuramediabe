import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin
from src.model.playback.media import MediaClip


class ClipCollection(TimestampedMixin, BaseModel):
    """片段合集：跨影片的有序片段集合，可连续播放。"""

    name = peewee.CharField(max_length=255, unique=True)
    description = peewee.TextField(default="")

    class Meta:
        table_name = "clip_collection"


class ClipCollectionItem(TimestampedMixin, BaseModel):
    """合集成员，用显式 position 维护连续播放顺序。"""

    collection = peewee.ForeignKeyField(ClipCollection, backref="items", on_delete="CASCADE")
    # 片段被删除时自动移出所有合集。
    clip = peewee.ForeignKeyField(MediaClip, backref="collection_items", on_delete="CASCADE")
    position = peewee.IntegerField(index=True)

    class Meta:
        table_name = "clip_collection_item"
        indexes = ((("collection", "clip"), True),)
