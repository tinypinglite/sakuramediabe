from typing import List, Optional, Set
from urllib.parse import urlparse

from src.api.exception.errors import ApiError
from src.config.config import (
    IndexerItem,
    IndexerKind,
    IndexerType,
    Settings,
    settings,
    update_settings as persist_settings,
)
from src.schema.system.indexer_settings import (
    IndexerItemUpdatePayload,
    IndexerSettingsResource,
    IndexerSettingsUpdateRequest,
)


class IndexerSettingsService:
    @staticmethod
    def get_settings() -> IndexerSettingsResource:
        return IndexerSettingsResource.from_settings(settings.indexer_settings)

    @classmethod
    def update_settings(
        cls,
        payload: IndexerSettingsUpdateRequest,
    ) -> IndexerSettingsResource:
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(
                422,
                "empty_indexer_settings_update",
                "At least one field must be provided",
            )

        current_settings = Settings.model_validate(settings.model_dump())
        indexer_settings = current_settings.indexer_settings.model_copy(deep=True)

        if "type" in update_data:
            indexer_settings.type = cls._validate_type(payload.type)

        if "api_key" in update_data:
            indexer_settings.api_key = cls._validate_api_key(payload.api_key)

        if "indexers" in update_data:
            indexer_settings.indexers = cls._validate_indexers(payload.indexers)

        current_settings.indexer_settings = indexer_settings
        persist_settings(current_settings)
        return cls.get_settings()

    @staticmethod
    def _validate_api_key(value: Optional[str]) -> str:
        if value is None:
            raise ApiError(
                422,
                "invalid_indexer_settings_api_key",
                "Indexer API key cannot be empty",
            )
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_api_key",
                "Indexer API key cannot be empty",
            )
        return normalized

    @staticmethod
    def _validate_name(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_name",
                "Indexer name cannot be empty",
            )
        return normalized

    @staticmethod
    def _validate_url(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_url",
                "Indexer URL cannot be empty",
            )

        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ApiError(
                422,
                "invalid_indexer_settings_url",
                "Indexer URL must use http or https",
                {"url": value},
            )
        return normalized

    @staticmethod
    def _validate_type(value: Optional[str]) -> IndexerType:
        if value is None:
            raise ApiError(
                422,
                "invalid_indexer_settings_type",
                "Indexer type cannot be empty",
            )
        normalized = value.strip().lower()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_type",
                "Indexer type cannot be empty",
            )

        try:
            return IndexerType(normalized)
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_indexer_settings_type",
                "Unsupported indexer type",
                {"type": value},
            ) from exc

    @staticmethod
    def _validate_kind(value: str) -> IndexerKind:
        normalized = value.strip().lower()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_kind",
                "Indexer kind cannot be empty",
            )

        try:
            return IndexerKind(normalized)
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_indexer_settings_kind",
                "Unsupported indexer kind",
                {"kind": value},
            ) from exc

    @classmethod
    def _validate_indexers(
        cls,
        items: Optional[List[IndexerItemUpdatePayload]],
    ) -> List[IndexerItem]:
        if items is None:
            raise ApiError(
                422,
                "invalid_indexer_settings_indexers",
                "Indexers must be a list",
            )
        normalized_names: Set[str] = set()
        indexers: List[IndexerItem] = []

        for item in items:
            name = cls._validate_name(item.name)
            if name in normalized_names:
                raise ApiError(
                    422,
                    "duplicate_indexer_settings_name",
                    "Indexer name must be unique",
                    {"name": name},
                )
            normalized_names.add(name)
            indexers.append(
                IndexerItem(
                    name=name,
                    url=cls._validate_url(item.url),
                    kind=cls._validate_kind(item.kind),
                )
            )

        return indexers
