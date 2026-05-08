import os

import peewee

from src.common.runtime_time import utc_now_for_db
from src.model.base import BaseModel, JsonTextField
from src.model.mixins import TimestampedMixin


class BackgroundTaskRun(TimestampedMixin, BaseModel):
    task_key = peewee.CharField(max_length=64, index=True)
    task_name = peewee.CharField(max_length=255)
    trigger_type = peewee.CharField(max_length=32, index=True)
    owner_pid = peewee.IntegerField(null=True, default=os.getpid)
    mutex_key = peewee.CharField(max_length=128, null=True)
    state = peewee.CharField(max_length=32, default="pending", index=True)
    progress_current = peewee.IntegerField(null=True)
    progress_total = peewee.IntegerField(null=True)
    progress_text = peewee.CharField(max_length=255, null=True)
    result_summary = JsonTextField(default=dict)
    result_text = peewee.TextField(null=True)
    error_message = peewee.TextField(null=True)
    started_at = peewee.DateTimeField(null=True)
    finished_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "background_task_run"
        indexes = (
            (("task_key", "created_at"), False),
            (("mutex_key",), True),
        )


class SystemNotification(TimestampedMixin, BaseModel):
    category = peewee.CharField(max_length=32, index=True)
    title = peewee.CharField(max_length=255)
    content = peewee.TextField()
    is_read = peewee.BooleanField(default=False, index=True)
    read_at = peewee.DateTimeField(null=True)
    archived_at = peewee.DateTimeField(null=True, index=True)
    related_task_run = peewee.ForeignKeyField(
        BackgroundTaskRun,
        null=True,
        backref="notifications",
        on_delete="SET NULL",
        column_name="related_task_run_id",
    )
    related_resource_type = peewee.CharField(max_length=64, null=True)
    related_resource_id = peewee.IntegerField(null=True)

    class Meta:
        table_name = "system_notification"


class SystemEvent(TimestampedMixin, BaseModel):
    event_type = peewee.CharField(max_length=64, index=True)
    resource_type = peewee.CharField(max_length=64, null=True, index=True)
    resource_id = peewee.IntegerField(null=True)
    payload = JsonTextField(default=dict)
    emitted_at = peewee.DateTimeField(default=utc_now_for_db, index=True)

    class Meta:
        table_name = "system_event"
