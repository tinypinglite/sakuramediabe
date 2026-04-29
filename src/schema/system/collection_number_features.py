from src.schema.common.base import SchemaModel


class CollectionNumberFeatureSyncStatsResource(SchemaModel):
    total_movies: int
    matched_count: int
    updated_to_collection_count: int
    updated_to_single_count: int
    unchanged_count: int


class CollectionNumberFeaturesResource(SchemaModel):
    features: list[str]
    sync_stats: CollectionNumberFeatureSyncStatsResource | None = None


class CollectionNumberFeaturesUpdateRequest(SchemaModel):
    features: list[str] | None = None
