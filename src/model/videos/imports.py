import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin
from src.model.playback.libraries import MediaLibrary
from src.model.system.activity import BackgroundTaskRun
from src.model.videos.collections import VideoCollection


class VideoImportJob(TimestampedMixin, BaseModel):
    # 非 JAV 视频目录导入作业：记录源路径、归属媒体库、导入模式与统计，并关联后台任务运行用于进度可观测。
    source_path = peewee.CharField(max_length=1024)
    library = peewee.ForeignKeyField(
        MediaLibrary,
        backref="video_import_jobs",
        on_delete="CASCADE",
        column_name="library_id",
    )
    task_run = peewee.ForeignKeyField(
        BackgroundTaskRun,
        null=True,
        backref="video_import_jobs",
        on_delete="SET NULL",
        column_name="task_run_id",
    )
    # 导入时可一并把视频追加进合集；合集被删时仅置空关联，不影响作业历史。
    collection = peewee.ForeignKeyField(
        VideoCollection,
        null=True,
        backref="video_import_jobs",
        on_delete="SET NULL",
        column_name="collection_id",
    )
    state = peewee.CharField(max_length=32, default="pending", index=True)
    # 导入模式：auto（硬链接优先，失败回退复制）/ cleanup-source（复制后删除源文件）。
    transfer_mode = peewee.CharField(max_length=32, default="auto")
    imported_count = peewee.IntegerField(default=0)
    skipped_count = peewee.IntegerField(default=0)
    failed_count = peewee.IntegerField(default=0)
    failed_files = peewee.TextField(default="[]")
    started_at = peewee.DateTimeField(null=True)
    finished_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "video_import_job"
