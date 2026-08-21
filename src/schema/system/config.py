from typing import Any, Literal

from src.schema.common.base import SchemaModel


class ConfigResource(SchemaModel):
    # values 为全部配置节的明文快照（含敏感字段，前端自律）。
    values: dict[str, Any]


class ConfigUpdateResource(SchemaModel):
    values: dict[str, Any]
    # 普通配置仅写盘，API 与 APS 两个进程都必须重启后才会读取新快照。
    restart_required: list[Literal["api", "aps"]]
