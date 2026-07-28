import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin
from src.model.system.activity import BackgroundTaskRun


class ResourceTaskAttempt(TimestampedMixin, BaseModel):
    """资源级任务的单次尝试历史，只增不改（终态回写同一行后不再变更）。

    重试/重置不清历史：投影表 ResourceTaskState 只保留最新状态，完整尝试链靠本表
    回溯；task_run 被活动清理删除后由 SET NULL 兜底，历史行本身不随清理消失。
    """

    task_key = peewee.CharField(max_length=64)
    resource_type = peewee.CharField(max_length=32)
    resource_id = peewee.IntegerField()
    task_run = peewee.ForeignKeyField(
        BackgroundTaskRun,
        null=True,
        backref="+",
        on_delete="SET NULL",
        column_name="task_run_id",
    )
    # 该资源生命周期内的第 N 次尝试（终身序号，不随 retry_round 重置）
    attempt_no = peewee.IntegerField()
    trigger_type = peewee.CharField(max_length=32, null=True)
    # running / succeeded / failed / deferred / aborted
    state = peewee.CharField(max_length=32, default="running")
    error_code = peewee.CharField(max_length=64, null=True)
    error_message = peewee.TextField(null=True)
    retryable = peewee.BooleanField(null=True)
    started_at = peewee.DateTimeField(null=True)
    finished_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "resource_task_attempt"
        indexes = (
            (("task_key", "resource_type", "resource_id"), False),
        )
