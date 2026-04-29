from collections.abc import Iterable

from src.api.exception.errors import ApiError
from src.common.movie_numbers import normalize_movie_number
from src.config.config import (
    Settings,
    refresh_runtime_settings,
    settings,
    update_settings as persist_settings,
)
from src.schema.system.collection_number_features import (
    CollectionNumberFeaturesResource,
    CollectionNumberFeaturesUpdateRequest,
)
from src.service.catalog.movie_collection_service import MovieCollectionService


class CollectionNumberFeaturesService:
    @staticmethod
    def _normalize_runtime_features(features: Iterable[str]) -> list[str]:
        normalized_features: set[str] = set()
        for feature in features:
            normalized = normalize_movie_number(str(feature))
            if normalized:
                normalized_features.add(normalized)
        return sorted(normalized_features)

    @staticmethod
    def _normalize_features_for_update(features: list[str] | None) -> list[str]:
        if features is None:
            raise ApiError(
                422,
                "invalid_collection_number_feature",
                "Features must be a list",
            )

        normalized_features: set[str] = set()
        for feature in features:
            normalized = normalize_movie_number(feature)
            if not normalized:
                raise ApiError(
                    422,
                    "invalid_collection_number_feature",
                    "Feature cannot be empty after normalization",
                    {"feature": feature},
                )
            normalized_features.add(normalized)
        return sorted(normalized_features)

    @classmethod
    def get_features(cls) -> CollectionNumberFeaturesResource:
        return CollectionNumberFeaturesResource(
            features=cls._normalize_runtime_features(settings.media.others_number_features),
            sync_stats=None,
        )

    @classmethod
    def update_features(
        cls,
        payload: CollectionNumberFeaturesUpdateRequest,
        apply_now: bool = True,
    ) -> CollectionNumberFeaturesResource:
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(
                422,
                "empty_collection_number_features_update",
                "At least one field must be provided",
            )

        normalized_features = cls._normalize_features_for_update(payload.features)
        current_runtime_settings = Settings.model_validate(settings.model_dump())
        next_runtime_settings = Settings.model_validate(settings.model_dump())
        next_runtime_settings.media.others_number_features = set(normalized_features)

        if not apply_now:
            persist_settings(next_runtime_settings)
            return CollectionNumberFeaturesResource(
                features=normalized_features,
                sync_stats=None,
            )

        refresh_runtime_settings(next_runtime_settings)
        try:
            sync_stats = MovieCollectionService.sync_movie_collections()
            persist_settings(next_runtime_settings)
        except Exception:
            refresh_runtime_settings(current_runtime_settings)
            raise

        return CollectionNumberFeaturesResource.model_validate(
            {
                "features": normalized_features,
                "sync_stats": sync_stats,
            }
        )
