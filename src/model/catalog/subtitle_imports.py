import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin
from src.model.system.activity import BackgroundTaskRun


class SubtitleImportJob(TimestampedMixin, BaseModel):
    """手动字幕目录导入作业。

    用户把按番号命名的 .srt 放进一个目录后，后台递归扫描并归档到影片字幕目录。
    字幕资产跟番号走、不归属媒体库，因此本表不设 library 外键。
    """

    source_path = peewee.CharField(max_length=1024)
    task_run = peewee.ForeignKeyField(
        BackgroundTaskRun,
        null=True,
        backref="subtitle_import_jobs",
        on_delete="SET NULL",
        column_name="task_run_id",
    )
    state = peewee.CharField(max_length=32, default="pending", index=True)
    # 导入模式固定 auto（硬链接优先、复制兜底、不删源）；保留字段是为了复用通用作业骨架。
    transfer_mode = peewee.CharField(max_length=32, default="auto")
    imported_count = peewee.IntegerField(default=0)
    skipped_count = peewee.IntegerField(default=0)
    failed_count = peewee.IntegerField(default=0)
    failed_files = peewee.TextField(default="[]")
    started_at = peewee.DateTimeField(null=True)
    finished_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "subtitle_import_job"
