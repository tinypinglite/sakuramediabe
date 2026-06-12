import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin
from src.model.videos.items import VideoItem


class VideoCollection(TimestampedMixin, BaseModel):
    # 视频合集，前端可按成员 position 顺序播放，区别于 JAV 的 Playlist。
    name = peewee.CharField(max_length=255, unique=True, index=True, verbose_name="合集名称")
    description = peewee.TextField(default="", verbose_name="合集描述")

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        return super().save(*args, **kwargs)

    class Meta:
        table_name = "video_collection"


class VideoCollectionItem(TimestampedMixin, BaseModel):
    # 合集成员，position 显式维护播放顺序（PlaylistMovie 缺少该字段，故新增）。
    collection = peewee.ForeignKeyField(VideoCollection, backref="items", on_delete="CASCADE")
    video_item = peewee.ForeignKeyField(VideoItem, backref="collection_links", on_delete="CASCADE")
    position = peewee.IntegerField(default=0, index=True, verbose_name="排序位置")

    class Meta:
        table_name = "video_collection_item"
        indexes = ((("collection", "video_item"), True),)
