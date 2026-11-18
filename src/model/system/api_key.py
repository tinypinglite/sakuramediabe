from peewee import CharField, DateTimeField

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin


class ApiKey(TimestampedMixin, BaseModel):
    """外部集成（如 MCP server）使用的 API 密钥。

    只存 SHA-256 哈希，明文仅在生成响应中出现一次；key_hint 为展示用前缀。
    """

    name = CharField(max_length=64, default="")
    key_hint = CharField(max_length=32)
    key_hash = CharField(unique=True, index=True)
    last_used_at = DateTimeField(null=True)

    class Meta:
        table_name = "api_keys"
