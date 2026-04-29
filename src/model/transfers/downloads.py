import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin
from src.model.playback.libraries import MediaLibrary
from src.model.system.activity import BackgroundTaskRun


class DownloadClient(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    base_url = peewee.CharField(max_length=255)
    username = peewee.CharField(max_length=255)
    password = peewee.CharField(max_length=255)
    client_save_path = peewee.CharField(max_length=1024)
    local_root_path = peewee.CharField(max_length=1024)
    media_library = peewee.ForeignKeyField(
        MediaLibrary,
        backref="download_clients",
        on_delete="CASCADE",
        column_name="media_library_id",
    )

    class Meta:
        table_name = "download_client"


class Indexer(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    url = peewee.CharField(max_length=1024)
    kind = peewee.CharField(max_length=32)
    download_client = peewee.ForeignKeyField(
        DownloadClient,
        backref="indexers",
        on_delete="CASCADE",
        column_name="download_client_id",
    )

    class Meta:
        table_name = "indexer"


class DownloadTask(TimestampedMixin, BaseModel):
    client = peewee.ForeignKeyField(
        DownloadClient,
        backref="download_tasks",
        on_delete="CASCADE",
        column_name="client_id",
    )
    movie = peewee.CharField(max_length=255, null=True, column_name="movie_number", index=True)
    name = peewee.CharField(max_length=255)
    info_hash = peewee.CharField(max_length=128)
    save_path = peewee.CharField(max_length=1024)
    progress = peewee.FloatField(default=0)
    download_state = peewee.CharField(max_length=32, default="downloading", index=True)
    import_status = peewee.CharField(max_length=32, default="pending", index=True)

    class Meta:
        table_name = "download_task"
        indexes = ((("client", "info_hash"), True),)


class ImportJob(TimestampedMixin, BaseModel):
    source_path = peewee.CharField(max_length=1024)
    library = peewee.ForeignKeyField(
        MediaLibrary,
        backref="import_jobs",
        on_delete="CASCADE",
        column_name="library_id",
    )
    download_task = peewee.ForeignKeyField(
        DownloadTask,
        null=True,
        backref="import_jobs",
        on_delete="SET NULL",
        column_name="download_task_id",
    )
    task_run = peewee.ForeignKeyField(
        BackgroundTaskRun,
        null=True,
        backref="import_jobs",
        on_delete="SET NULL",
        column_name="task_run_id",
    )
    state = peewee.CharField(max_length=32, default="pending", index=True)
    imported_count = peewee.IntegerField(default=0)
    skipped_count = peewee.IntegerField(default=0)
    failed_count = peewee.IntegerField(default=0)
    failed_files = peewee.TextField(default="[]")
    started_at = peewee.DateTimeField(null=True)
    finished_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "import_job"
