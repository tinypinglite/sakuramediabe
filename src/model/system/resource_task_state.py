import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.mixins import TimestampedMixin


class ResourceTaskState(TimestampedMixin, BaseModel):
    task_key = peewee.CharField(max_length=64)
    resource_type = peewee.CharField(max_length=32)
    resource_id = peewee.IntegerField(index=True)
    state = peewee.CharField(max_length=32, default="pending", index=True)
    attempt_count = peewee.IntegerField(default=0)
    last_attempted_at = peewee.DateTimeField(null=True)
    last_succeeded_at = peewee.DateTimeField(null=True)
    last_error = peewee.TextField(null=True)
    last_error_at = peewee.DateTimeField(null=True)
    last_task_run_id = peewee.IntegerField(null=True)
    last_trigger_type = peewee.CharField(max_length=32, null=True)
    extra = JsonTextField(null=True, default=None)

    class Meta:
        table_name = "resource_task_state"
        indexes = (
            (("task_key", "resource_type", "resource_id"), True),
            (("task_key", "state", "updated_at"), False),
            (("resource_type", "resource_id"), False),
        )
