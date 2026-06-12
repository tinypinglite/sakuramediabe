import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.catalog.images import Image
from src.model.mixins import TimestampedMixin


class Person(TimestampedMixin, BaseModel):
    # 非 JAV 资源里的「人物」，不复用 JAV 强耦合的 Actor（无 javdb_id / 订阅语义）。
    name = peewee.CharField(max_length=255, index=True, verbose_name="人物名字")
    avatar_image = peewee.ForeignKeyField(
        Image,
        null=True,
        backref="persons",
        on_delete="SET NULL",
        verbose_name="头像图片",
    )
    gender = peewee.IntegerField(default=0, verbose_name="性别")
    extra = JsonTextField(null=True, default=None, verbose_name="额外元数据")

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        return super().save(*args, **kwargs)

    @property
    def avatar_url(self) -> str | None:
        if self.avatar_image_id and self.avatar_image:
            return self.avatar_image.medium
        return None

    class Meta:
        table_name = "person"
