from datetime import datetime
from typing import List, Optional

from src.config.config import IndexerKind, IndexerType
from src.schema.common.base import SchemaModel


class IndexerBoundClientResource(SchemaModel):
    # 索引器绑定的单个下载器概要：kind 供前端展示入口种类（qbittorrent / cloud115）。
    id: int
    name: str
    kind: str


class IndexerItemResource(SchemaModel):
    id: int
    name: str
    url: str
    kind: IndexerKind
    # 多对多绑定：按绑定顺序列出；提交下载时按全局 kind 偏好从中挑选。
    download_clients: List[IndexerBoundClientResource]


class IndexerSettingsResource(SchemaModel):
    type: IndexerType
    api_key: str
    indexers: List[IndexerItemResource]


class IndexerItemUpdatePayload(SchemaModel):
    name: str
    url: str
    kind: str
    # 至少绑定一个下载器；重复 id 会被拒绝；PT 索引器不能绑定 cloud115。
    download_client_ids: List[int]


class IndexerSettingsUpdateRequest(SchemaModel):
    type: Optional[str] = None
    api_key: Optional[str] = None
    indexers: Optional[List[IndexerItemUpdatePayload]] = None


class IndexerConnectionTestError(SchemaModel):
    type: str
    message: str


class IndexerConnectionTestResponse(SchemaModel):
    healthy: bool
    checked_at: datetime
    query: str
    indexers_checked: int
    result_count: int
    elapsed_ms: int
    error: Optional[IndexerConnectionTestError] = None
