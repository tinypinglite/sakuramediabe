from datetime import datetime

from src.config.config import IndexerKind
from src.schema.common.base import SchemaModel


class IndexerBoundClientResource(SchemaModel):
    # 索引器绑定的单个下载器概要。
    id: int
    name: str


class IndexerItemResource(SchemaModel):
    id: int
    name: str
    # Torznab 搜索接口地址。
    url: str
    kind: IndexerKind
    # 每个索引器独立的 Torznab 鉴权 key；为空表示请求不带 apikey。明文返回，前端自律。
    api_key: str | None = None
    # 多对多绑定：按绑定顺序列出；提交下载时按全局 kind 偏好从中挑选。
    download_clients: list[IndexerBoundClientResource]


class IndexerSettingsResource(SchemaModel):
    indexers: list[IndexerItemResource]


class IndexerItemUpdatePayload(SchemaModel):
    name: str
    url: str
    kind: str
    # 可选鉴权 key：空串/空白会归一为 None（不携带 apikey）。
    api_key: str | None = None
    # 至少绑定一个下载器；重复 id 会被拒绝。
    download_client_ids: list[int]


class IndexerSettingsUpdateRequest(SchemaModel):
    # 兼容旧版前端：升级前的全局 type/api_key 已废弃，保留字段但忽略不生效（避免旧请求 422）。
    type: str | None = None
    api_key: str | None = None
    indexers: list[IndexerItemUpdatePayload] | None = None


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
    error: IndexerConnectionTestError | None = None
