from typing import List, Optional

from src.config.config import IndexerKind, IndexerType
from src.schema.common.base import SchemaModel


class IndexerItemResource(SchemaModel):
    id: int
    name: str
    url: str
    kind: IndexerKind
    download_client_id: int
    download_client_name: str


class IndexerSettingsResource(SchemaModel):
    type: IndexerType
    api_key: str
    indexers: List[IndexerItemResource]


class IndexerItemUpdatePayload(SchemaModel):
    name: str
    url: str
    kind: str
    download_client_id: int


class IndexerSettingsUpdateRequest(SchemaModel):
    type: Optional[str] = None
    api_key: Optional[str] = None
    indexers: Optional[List[IndexerItemUpdatePayload]] = None
