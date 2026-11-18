from datetime import datetime

from pydantic import Field

from src.schema.common.base import SchemaModel


class ApiKeyResource(SchemaModel):
    id: int
    name: str
    key_hint: str
    created_at: datetime
    last_used_at: datetime | None = None


class ApiKeyCreateRequest(SchemaModel):
    name: str = Field(default="", max_length=64)


class ApiKeyCreatedResource(ApiKeyResource):
    """生成响应：key 明文仅此一次返回，之后不可再获取。"""

    key: str
