from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from src.config.config import IndexerKind
from src.schema.common.base import SchemaModel
from src.schema.transfers.downloads import DownloadCandidateResource

GIB = 1024**3


class DownloadSelectionRanking(str, Enum):
    SIZE_DESC = "size_desc"
    SEEDERS_DESC = "seeders_desc"
    PUBLISHED_AT_DESC = "published_at_desc"
    INDEXER_PRIORITY = "indexer_priority"


class DownloadSelectionKeywordGroup(SchemaModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )

    label: str | None = Field(default=None, max_length=80)
    terms: list[str] = Field(min_length=1, max_length=50)

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value):
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("terms")
    @classmethod
    def normalize_terms(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            term = str(value).strip()
            if not term:
                raise ValueError("keyword term cannot be blank")
            if len(term) > 100:
                raise ValueError("keyword term cannot exceed 100 characters")
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(term)
        return normalized


def _normalize_string_list(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if not item:
            raise ValueError("list item cannot be blank")
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return normalized


class DownloadSelectionRules(SchemaModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )

    schema_version: Literal[1] = 1
    indexer_kinds: list[IndexerKind] = Field(default_factory=list)
    indexer_names: list[str] = Field(default_factory=list, max_length=100)
    min_size_bytes: int = Field(default=1 * GIB, ge=0)
    max_size_bytes: int | None = Field(default=40 * GIB, gt=0)
    min_seeders: int = Field(default=1, ge=0)
    max_age_days: int | None = Field(default=None, ge=1, le=36500)
    match_title: bool = True
    match_description: bool = True
    required_groups: list[DownloadSelectionKeywordGroup] = Field(
        default_factory=list,
        max_length=20,
    )
    excluded_terms: list[str] = Field(default_factory=list, max_length=100)
    preferred_groups: list[DownloadSelectionKeywordGroup] = Field(
        default_factory=list,
        max_length=20,
    )
    ranking: list[DownloadSelectionRanking] = Field(
        default_factory=lambda: [DownloadSelectionRanking.SIZE_DESC],
        max_length=4,
    )
    indexer_priority_names: list[str] = Field(default_factory=list, max_length=100)

    @field_validator(
        "indexer_names",
        "excluded_terms",
        "indexer_priority_names",
    )
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(str(value).strip()) > 255:
                raise ValueError("list item cannot exceed 255 characters")
        return _normalize_string_list(values)

    @model_validator(mode="after")
    def validate_rules(self):
        self.indexer_kinds = list(dict.fromkeys(self.indexer_kinds))
        self.ranking = list(dict.fromkeys(self.ranking))
        if self.max_size_bytes is not None and self.max_size_bytes < self.min_size_bytes:
            raise ValueError("max_size_bytes must be greater than or equal to min_size_bytes")
        if not self.match_title and not self.match_description:
            raise ValueError("at least one match field must be enabled")
        if (
            DownloadSelectionRanking.INDEXER_PRIORITY in self.ranking
            and not self.indexer_priority_names
        ):
            raise ValueError("indexer_priority ranking requires indexer_priority_names")
        return self


class DownloadSelectionPolicyResource(SchemaModel):
    rules: DownloadSelectionRules
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, policy) -> DownloadSelectionPolicyResource:
        return cls.model_validate(
            {
                "rules": DownloadSelectionRules.model_validate(policy.rules or {}),
                "version": policy.version,
                "created_at": policy.created_at,
                "updated_at": policy.updated_at,
            }
        )


class DownloadSelectionPolicyUpdateRequest(SchemaModel):
    expected_version: int = Field(ge=1)
    rules: DownloadSelectionRules


class DownloadSelectionPreviewRequest(SchemaModel):
    movie_number: str = Field(min_length=1, max_length=255)
    rules: DownloadSelectionRules | None = None


class DownloadSelectionPreviewResponse(SchemaModel):
    draft: bool
    policy_version: int | None = None
    raw_candidate_count: int
    system_candidate_count: int
    eligible_candidate_count: int
    policy_filtered_count: int
    rejection_counts: dict[str, int]
    selected_source_uri: str | None = None
    candidates: list[DownloadCandidateResource]
