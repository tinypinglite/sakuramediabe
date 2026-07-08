from enum import Enum
from typing import Any, Literal

from src.schema.common.base import SchemaModel


class ConfigEffectLevel(str, Enum):
    """配置项修改后的生效方式。

    - HOT：api 进程内即时生效（每次使用现读 settings，或依赖缓存已在 refresh 时清理）。
    - RESTART_API：需重启 api 进程（连接池/日志/文档等在启动期读取一次）。
    - RESTART_SCHEDULER：需重启 aps 调度进程（cron 装配时烘进 CronTrigger，或由定时任务消费）。
    """

    HOT = "hot"
    RESTART_API = "restart_api"
    RESTART_SCHEDULER = "restart_scheduler"


class ConfigResource(SchemaModel):
    # values 为全部配置节的明文快照（含敏感字段，前端自律）；effects 为每节/字段的生效方式。
    values: dict[str, Any]
    effects: dict[str, ConfigEffectLevel]


class PendingRestartField(SchemaModel):
    # 本次修改中不能即时生效、需重启对应进程才生效的字段。
    field: str
    restart: Literal["api", "scheduler"]


class ConfigUpdateResource(SchemaModel):
    values: dict[str, Any]
    # applied：本次已即时生效（hot）的字段；pending_restart：需重启才生效的字段。
    applied: list[str]
    pending_restart: list[PendingRestartField]
