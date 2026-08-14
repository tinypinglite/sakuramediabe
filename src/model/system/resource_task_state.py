import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.mixins import TimestampedMixin
from src.model.system.activity import BackgroundTaskRun
from src.model.system.resource_task_attempt import ResourceTaskAttempt


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
    # 外键化（列名保持 last_task_run_id，object-id 访问器不变）；活动清理删 run 时
    # SET NULL，取代此前裸整数悬空的问题。
    last_task_run = peewee.ForeignKeyField(
        BackgroundTaskRun,
        null=True,
        backref="+",
        on_delete="SET NULL",
        column_name="last_task_run_id",
    )
    last_trigger_type = peewee.CharField(max_length=32, null=True)
    extra = JsonTextField(null=True, default=None)
    # 任务架构 Wave 0 扩展列（见 docs/development/task-architecture.md）：
    # next_retry_at 让重试节奏与 cron 解耦；error_code 取代解析 last_error 字符串与
    # extra.terminal 子串匹配；retry_round 重开预算时递增、attempt_count 只记本轮
    # 失败次数（成功清零、基础设施失败回滚，终身次数看 attempt 表）。
    next_retry_at = peewee.DateTimeField(null=True)
    error_code = peewee.CharField(max_length=64, null=True)
    retry_round = peewee.IntegerField(default=0)
    last_attempt = peewee.ForeignKeyField(
        ResourceTaskAttempt,
        null=True,
        backref="+",
        on_delete="SET NULL",
        column_name="last_attempt_id",
    )

    class Meta:
        table_name = "resource_task_state"
        indexes = (
            (("task_key", "resource_type", "resource_id"), True),
            (("task_key", "state", "updated_at"), False),
            (("resource_type", "resource_id"), False),
            # 候选/重试调度路径：state IN (...) AND next_retry_at <= now
            (("task_key", "state", "next_retry_at"), False),
        )
