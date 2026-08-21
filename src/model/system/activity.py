import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.mixins import TimestampedMixin


class BackgroundTaskRun(TimestampedMixin, BaseModel):
    task_key = peewee.CharField(max_length=64, index=True)
    task_name = peewee.CharField(max_length=255)
    trigger_type = peewee.CharField(max_length=32, index=True)
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
    # pending 行即队列元素；lease_expires_at 过期即可回收。
    params = JsonTextField(null=True, default=None)
    scheduled_at = peewee.DateTimeField(null=True)
    lease_expires_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "background_task_run"
        indexes = (
            (("task_key", "created_at"), False),
            (("mutex_key",), True),
            # 队列领取路径：WHERE state='pending' AND scheduled_at <= now ORDER BY id
            (("state", "scheduled_at"), False),
        )


class SystemNotification(TimestampedMixin, BaseModel):
    category = peewee.CharField(max_length=32, index=True)
    title = peewee.CharField(max_length=255)
    content = peewee.TextField()
    # 事件身份与展示关联分离：旧 related_resource_* 继续服务现有 API。
    event_type = peewee.CharField(max_length=64, null=True)
    dedupe_key = peewee.CharField(max_length=255, null=True, unique=True)
    resource_type = peewee.CharField(max_length=64, null=True)
    resource_id = peewee.IntegerField(null=True)
    is_read = peewee.BooleanField(default=False, index=True)
    read_at = peewee.DateTimeField(null=True)
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
