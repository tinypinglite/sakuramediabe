from datetime import date

import peewee

from src.common.runtime_time import utc_now_for_db
from src.model.base import BaseModel, CaseSensitiveCharField, JsonbField
from src.model.catalog.images import Image
from src.model.mixins import TimestampedMixin

ACTOR_FIELD_CODECS = {
    "birthday": date,
    "height_cm": int,
    "bust_cm": int,
    "waist_cm": int,
    "hips_cm": int,
    "cup": str,
    "birthplace": str,
    "blood_type": str,
}
PROTECTED_ACTOR_FIELDS = frozenset(ACTOR_FIELD_CODECS)
_GUARDED_FIELDS = PROTECTED_ACTOR_FIELDS | {"field_owners", "mutation_revision"}


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
    subscribed_at = peewee.DateTimeField(null=True, index=True)
    subscribed_movies_synced_at = peewee.DateTimeField(null=True, index=True)
    subscribed_movies_full_synced_at = peewee.DateTimeField(null=True, index=True)
    birthday = peewee.DateField(null=True)
    height_cm = peewee.IntegerField(null=True)
    bust_cm = peewee.IntegerField(null=True)
    waist_cm = peewee.IntegerField(null=True)
    hips_cm = peewee.IntegerField(null=True)
    cup = peewee.CharField(max_length=255, null=True)
    birthplace = peewee.CharField(max_length=255, null=True)
    blood_type = peewee.CharField(max_length=255, null=True)
    field_owners = JsonbField(default=dict, constraints=[peewee.SQL("DEFAULT '{}'::jsonb")])
    mutation_revision = peewee.BigIntegerField(default=0, constraints=[peewee.SQL("DEFAULT 0")])

    def save(self, *args, **kwargs):
        if self._pk is not None and not kwargs.get("force_insert", False):
            only = kwargs.get("only")
            if not only:
                raise RuntimeError("已持久化 Actor 的 save() 必须传 only")
            self._guard_fields(only)
        self.javdb_id = (self.javdb_id or "").strip()
        self.name = (self.name or "").strip()
        self.alias_name = (self.alias_name or "").strip()
        return super().save(*args, **kwargs)

    @staticmethod
    def _guard_fields(fields):
        names = {field.name if isinstance(field, peewee.Field) else field for field in fields}
        protected = _GUARDED_FIELDS & names
        if protected:
            raise RuntimeError(f"受保护字段禁止直接写入: {sorted(protected)}")

    @classmethod
    def update(cls, *args, **kwargs):
        if args and isinstance(args[0], dict):
            cls._guard_fields(args[0])
        cls._guard_fields(kwargs)
        return super().update(*args, **kwargs)

    @property
    def age(self) -> int | None:
        if self.birthday is None:
            return None
        today = utc_now_for_db().date()
        return today.year - self.birthday.year - (
            (today.month, today.day) < (self.birthday.month, self.birthday.day)
        )

    @property
    def avatar_url(self) -> str | None:
        if self.profile_image_id and self.profile_image:
            return self.profile_image.medium
        return None

    class Meta:
        table_name = "actor"
