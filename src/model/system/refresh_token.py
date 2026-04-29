from peewee import CharField, DateTimeField

from src.model.base import BaseModel
from src.model.enums import RefreshTokenStatus
from src.model.mixins import TimestampedMixin


class UserRefreshToken(TimestampedMixin, BaseModel):
    token_id = CharField(unique=True, index=True)
    token_hash = CharField()
    status = CharField(max_length=32, default=RefreshTokenStatus.ACTIVE.value)
    expires_at = DateTimeField()
    revoked_at = DateTimeField(null=True)
    replaced_by_token_id = CharField(null=True)
    client_ip = CharField(null=True)
    user_agent = CharField(null=True)

    class Meta:
        table_name = "user_refresh_tokens"
