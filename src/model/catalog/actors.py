import peewee

from src.model.catalog.images import Image
from src.model.base import BaseModel, CaseSensitiveCharField
from src.model.mixins import TimestampedMixin


class Actor(TimestampedMixin, BaseModel):
    javdb_id = CaseSensitiveCharField(max_length=64, unique=True, index=True, verbose_name="JavDB ID")
    name = peewee.CharField(index=True, verbose_name="演员名字")
    alias_name = peewee.TextField(default="", verbose_name="别名")
    profile_image = peewee.ForeignKeyField(
        Image,
        null=True,
        backref="actors",
        on_delete="SET NULL",
        verbose_name="头像图片",
    )
    javdb_type = peewee.IntegerField(default=0, verbose_name="JavDB 类型")
    gender = peewee.IntegerField(default=0, verbose_name="性别")
    is_subscribed = peewee.BooleanField(default=False, index=True)
    subscribed_movies_synced_at = peewee.DateTimeField(null=True, index=True)
    subscribed_movies_full_synced_at = peewee.DateTimeField(null=True, index=True)

    def save(self, *args, **kwargs):
        self.javdb_id = (self.javdb_id or "").strip()
        self.name = (self.name or "").strip()
        self.alias_name = (self.alias_name or "").strip()
        return super().save(*args, **kwargs)

    @property
    def avatar_url(self) -> str | None:
        if self.profile_image_id and self.profile_image:
            return self.profile_image.medium
        return None

    class Meta:
        table_name = "actor"
