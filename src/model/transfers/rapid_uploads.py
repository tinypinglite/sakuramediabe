import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin
from src.model.playback.libraries import MediaLibrary
from src.model.playback.media import Media
from src.model.system.activity import BackgroundTaskRun, SystemNotification


class MediaRapidUploadBatch(TimestampedMixin, BaseModel):
    """一次由前端触发的批量本地媒体秒传作业。"""

    target_library = peewee.ForeignKeyField(
        MediaLibrary,
        backref="rapid_upload_batches",
        on_delete="RESTRICT",
        column_name="target_library_id",
    )
    retry_of_batch = peewee.ForeignKeyField(
        "self",
        null=True,
        backref="retry_batches",
        on_delete="SET NULL",
        column_name="retry_of_batch_id",
    )
    task_run = peewee.ForeignKeyField(
        BackgroundTaskRun,
        null=True,
        backref="rapid_upload_batches",
        on_delete="SET NULL",
        column_name="task_run_id",
    )
    completion_notification = peewee.ForeignKeyField(
        SystemNotification,
        null=True,
        backref="rapid_upload_batches",
        on_delete="SET NULL",
        column_name="completion_notification_id",
    )
    state = peewee.CharField(max_length=32, default="pending", index=True)
    total_count = peewee.IntegerField(default=0)
    succeeded_count = peewee.IntegerField(default=0)
    failed_count = peewee.IntegerField(default=0)
    cleanup_failed_count = peewee.IntegerField(default=0)
    started_at = peewee.DateTimeField(null=True)
    finished_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "media_rapid_upload_batch"


class MediaRapidUploadItem(TimestampedMixin, BaseModel):
    """批次中的单条媒体；保存跨 115、数据库和本地文件系统的恢复信息。"""

    batch = peewee.ForeignKeyField(
        MediaRapidUploadBatch,
        backref="items",
        on_delete="CASCADE",
        column_name="batch_id",
    )
    media = peewee.ForeignKeyField(
        Media,
        null=True,
        backref="rapid_upload_items",
        on_delete="SET NULL",
        column_name="media_id",
    )
    # 活动作业持有 media id；进入终态后置空。唯一约束阻止同一媒体并发进入多个批次。
    active_media_id = peewee.IntegerField(null=True, unique=True)
    action = peewee.CharField(max_length=32, default="rapid_upload")
    state = peewee.CharField(max_length=32, default="pending", index=True)
    source_library_id = peewee.IntegerField(null=True)
    source_path = peewee.CharField(max_length=1024)
    source_size_bytes = peewee.BigIntegerField(default=0)
    source_mtime_ns = peewee.BigIntegerField(default=0)
    source_device = peewee.BigIntegerField(default=0)
    source_inode = peewee.BigIntegerField(default=0)
    source_sha1 = peewee.CharField(max_length=40, null=True)
    target_cid = peewee.CharField(max_length=64, null=True)
    target_fid = peewee.CharField(max_length=64, null=True)
    target_pickcode = peewee.CharField(max_length=128, null=True)
    target_name = peewee.CharField(max_length=255, null=True)
    error_message = peewee.TextField(null=True)
    started_at = peewee.DateTimeField(null=True)
    finished_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "media_rapid_upload_item"
        indexes = (
            (("batch", "media"), True),
            (("batch", "state"), False),
        )
