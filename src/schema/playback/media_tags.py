from __future__ import annotations

import unicodedata
from datetime import datetime

from pydantic import Field, field_validator

from src.schema.common.base import SchemaModel
from src.schema.common.media_tags import MediaTagBriefResource


def _normalize_terms(value):
    if not isinstance(value, list):
        return value
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            # Pydantic v2 does not convert TypeError from validators to ValidationError.
            raise ValueError("媒体标签词条必须是字符串")  # noqa: TRY004
        term = item.strip()
        if not term:
            raise ValueError("媒体标签词条不能为空")
        if len(term) > 100:
            raise ValueError("媒体标签词条不能超过 100 个字符")
        key = unicodedata.normalize("NFKC", term).casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(term)
    return normalized


class MediaTagResource(MediaTagBriefResource):
    include_terms: list[str] = Field(default_factory=list)
    exclude_terms: list[str] = Field(default_factory=list)
    enabled: bool = True
    media_count: int = 0
    created_at: datetime
    updated_at: datetime


class MediaTagUpsertRequest(SchemaModel):
    name: str = Field(min_length=1, max_length=64)
    include_terms: list[str] = Field(min_length=1, max_length=50)
    exclude_terms: list[str] = Field(default_factory=list, max_length=50)
    enabled: bool = True

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("include_terms", "exclude_terms", mode="before")
    @classmethod
    def validate_terms(cls, value):
        return _normalize_terms(value)


class MediaTagPreviewRequest(SchemaModel):
    file_name: str = Field(min_length=1, max_length=1024)
    release_title: str | None = None
    release_description: str | None = None

    @field_validator(
        "file_name",
        "release_title",
        "release_description",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class MediaTagPreviewMatchResource(SchemaModel):
    tag_id: int
    name: str
    matched_term: str


class MediaTagPreviewResponse(SchemaModel):
    matches: list[MediaTagPreviewMatchResource] = Field(default_factory=list)
