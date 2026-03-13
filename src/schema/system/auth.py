from datetime import datetime

from src.schema.common.base import SchemaModel


class TokenCreateRequest(SchemaModel):
    username: str
    password: str


class TokenRefreshRequest(SchemaModel):
    refresh_token: str


class AuthUserSummary(SchemaModel):
    username: str


class TokenResource(SchemaModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    expires_at: datetime
    refresh_expires_at: datetime
    user: AuthUserSummary
