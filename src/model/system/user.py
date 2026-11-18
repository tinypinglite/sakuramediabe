from peewee import CharField, DateTimeField

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin


class User(TimestampedMixin, BaseModel):
    username = CharField(unique=True, index=True)
    password_hash = CharField()
    # NULL 表示尚未选择；合法值由应用层归一化为 zh-CN / en。
    language = CharField(max_length=16, null=True)
    last_login_at = DateTimeField(null=True)

    class Meta:
        table_name = "users"
