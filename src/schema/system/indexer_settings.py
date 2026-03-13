from typing import List, Optional

from src.config.config import IndexerKind, IndexerType
from src.schema.common.base import SchemaModel


class IndexerItemResource(SchemaModel):
    name: str
    url: str
    kind: IndexerKind


class IndexerSettingsResource(SchemaModel):
    type: IndexerType
    api_key: str
    indexers: List[IndexerItemResource]

    @classmethod
    def from_settings(cls, indexer_settings) -> "IndexerSettingsResource":
        return cls.model_validate(indexer_settings.model_dump())


class IndexerItemUpdatePayload(SchemaModel):
    name: str
    url: str
    kind: str


class IndexerSettingsUpdateRequest(SchemaModel):
    type: Optional[str] = None
    api_key: Optional[str] = None
    indexers: Optional[List[IndexerItemUpdatePayload]] = None
