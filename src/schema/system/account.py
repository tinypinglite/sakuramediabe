from datetime import datetime

from src.schema.common.base import SchemaModel


class AccountResource(SchemaModel):
    username: str
    created_at: datetime
    last_login_at: datetime | None = None


class AccountUpdateRequest(SchemaModel):
    username: str


class AccountPasswordChangeRequest(SchemaModel):
    current_password: str
    new_password: str
