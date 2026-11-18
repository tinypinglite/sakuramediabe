from datetime import datetime

from pydantic import model_validator

from src.schema.common.base import SchemaModel


class AccountResource(SchemaModel):
    username: str
    language: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None


class AccountUpdateRequest(SchemaModel):
    username: str | None = None
    language: str | None = None

    @model_validator(mode="after")
    def _validate_patch_fields(self) -> "AccountUpdateRequest":
        if self.username is None and self.language is None:
            raise ValueError("必须提供 username 或 language 之一")
        if "username" in self.model_fields_set and self.username is None:
            raise ValueError("username 不能为 null")
        if "language" in self.model_fields_set and self.language is None:
            raise ValueError("language 不能为 null")
        return self


class AccountPasswordChangeRequest(SchemaModel):
    current_password: str
    new_password: str
