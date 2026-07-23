from src.schema.common.base import SchemaModel
from src.schema.system.activity import TaskRunResource


class JobMetadataResource(SchemaModel):
    task_key: str
    plugin_id: str | None = None
    log_name: str
    cli_name: str
    cli_help: str
    cron_setting: str
    cron_expr: str
    manual_trigger_allowed: bool
    last_task_run: TaskRunResource | None = None


class ManualJobTriggerResponse(SchemaModel):
    task_run_id: int
    task_key: str
    state: str
