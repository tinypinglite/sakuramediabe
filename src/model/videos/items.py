import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.catalog.images import Image
from src.model.catalog.tags import Tag
from src.model.mixins import TimestampedMixin
from src.model.videos.persons import Person


class VideoItem(TimestampedMixin, BaseModel):
    # 非 JAV 视频条目（无番号、无外部元数据），与 Movie 完全平行，文件归 Media。
    title = peewee.CharField(max_length=255, index=True, verbose_name="标题")
    summary = peewee.TextField(default="", verbose_name="描述")
    cover_image = peewee.ForeignKeyField(
        Image,
        null=True,
        backref="video_items_as_cover",
        on_delete="SET NULL",
        verbose_name="封面图片",
    )
    release_date = peewee.DateTimeField(null=True, index=True, verbose_name="发布时间")
    extra = JsonTextField(null=True, default=None, verbose_name="额外元数据")

    def save(self, *args, **kwargs):
        self.title = (self.title or "").strip()
        return super().save(*args, **kwargs)

    @property
    def cover_url(self) -> str | None:
        if self.cover_image_id and self.cover_image:
            return self.cover_image.medium
        return None

    class Meta:
        table_name = "video_item"


class VideoItemPerson(BaseModel):
    # 视频条目与人物的多对多关联。
    video_item = peewee.ForeignKeyField(VideoItem, backref="video_item_person_links", on_delete="CASCADE")
    person = peewee.ForeignKeyField(Person, backref="video_item_person_links", on_delete="CASCADE")

    class Meta:
        table_name = "video_item_person"
        indexes = ((("video_item", "person"), True),)


class VideoItemTag(BaseModel):
    # 视频条目复用 catalog 的通用 Tag，避免再造一套标签体系。
    video_item = peewee.ForeignKeyField(VideoItem, backref="video_item_tag_links", on_delete="CASCADE")
    tag = peewee.ForeignKeyField(Tag, backref="video_item_tag_links", on_delete="CASCADE")

    class Meta:
        table_name = "video_item_tag"
        indexes = ((("video_item", "tag"), True),)
