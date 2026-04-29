from peewee import CharField, DateTimeField

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin


class User(TimestampedMixin, BaseModel):
    username = CharField(unique=True, index=True)
    password_hash = CharField()
    last_login_at = DateTimeField(null=True)

    class Meta:
        table_name = "users"
