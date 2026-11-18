from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlsplit

from pydantic import Field, field_serializer, field_validator, model_validator

from src.schema.common.base import SchemaModel


class AnnouncementLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AnnouncementComponent(str, Enum):
    ALL = "all"
    FRONTEND = "frontend"
    BACKEND = "backend"


class StatusAnnouncementResource(SchemaModel):
    id: str
    level: AnnouncementLevel
    title_i18n: dict[str, str]
    body_i18n: dict[str, str]
    published_at: datetime
    expires_at: datetime | None = None
    component: AnnouncementComponent = AnnouncementComponent.ALL
    affected_from_version: str | None = None
    resolved_in_version: str | None = None
    link: str | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("announcement id must not be empty")
        return normalized

    @field_validator("title_i18n", "body_i18n")
    @classmethod
    def _validate_i18n(cls, value: dict[str, str]) -> dict[str, str]:
        for locale in ("zh-CN", "en"):
            text = value.get(locale, "").strip()
            if not text:
                raise ValueError(f"announcement text must include {locale}")
            value[locale] = text
        return value

    @field_validator("published_at", "expires_at")
    @classmethod
    def _normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_validator("link")
    @classmethod
    def _validate_link(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("announcement link must be a valid https URL")
        return normalized

    @field_serializer("published_at", "expires_at", when_used="json")
    def _serialize_utc_datetime(self, value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @model_validator(mode="after")
    def _validate_bounds(self):
        if self.component == AnnouncementComponent.ALL and (
            self.affected_from_version is not None
            or self.resolved_in_version is not None
        ):
            raise ValueError("component=all does not accept version bounds")
        if (
            self.expires_at is not None
            and self.expires_at <= self.published_at
        ):
            raise ValueError("expires_at must be later than published_at")
        return self


class StatusAnnouncementsResource(SchemaModel):
    items: list[StatusAnnouncementResource] = Field(default_factory=list)
