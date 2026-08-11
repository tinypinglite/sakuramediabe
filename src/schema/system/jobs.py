from src.schema.common.base import SchemaModel
from src.schema.system.activity import TaskRunResource


class JobMetadataResource(SchemaModel):
    task_key: str
    plugin_id: str | None = None
    log_name: str
    cli_name: str
    cli_help: str
    # manual_only 任务没有 cron 配置与表达式，返回 null。
    cron_setting: str | None = None
    cron_expr: str | None = None
    manual_trigger_allowed: bool
    # 有参数任务时输出 JSON Schema，前端据此渲染请求体表单。
    params_schema: dict | None = None
    last_task_run: TaskRunResource | None = None


class ManualJobTriggerResponse(SchemaModel):
    task_run_id: int
    task_key: str
    state: str
