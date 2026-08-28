import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.mixins import TimestampedMixin
from src.model.playback.libraries import MediaLibrary
from src.model.system.activity import BackgroundTaskRun


class DownloadClient(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    # provider 负责解释配置；宿主只保存并原样传回。
    provider_config = JsonTextField(default=dict)
    library = peewee.ForeignKeyField(
        MediaLibrary,
        backref="download_clients",
        on_delete="CASCADE",
        column_name="library_id",
    )

    class Meta:
        table_name = "download_client"


class Indexer(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    # Torznab 搜索接口地址；鉴权 key 可选，随每个索引器独立配置（为空则不携带 apikey 参数）。
    url = peewee.CharField(max_length=1024)
    # 索引器种类（IndexerKind）：pt / bt。
    kind = peewee.CharField(max_length=32)
    # 每个索引器自己的 Torznab 鉴权 key，可空；为空时搜索请求不带 apikey。
    api_key = peewee.CharField(max_length=255, null=True)

    class Meta:
        table_name = "indexer"


class IndexerDownloadClient(TimestampedMixin, BaseModel):
    """索引器与下载器的多对多绑定。"""

    indexer = peewee.ForeignKeyField(
        Indexer,
        backref="client_links",
        on_delete="CASCADE",
        column_name="indexer_id",
    )
    download_client = peewee.ForeignKeyField(
        DownloadClient,
        backref="indexer_links",
        on_delete="CASCADE",
        column_name="download_client_id",
    )

    class Meta:
        table_name = "indexer_download_client"
        indexes = ((('indexer', 'download_client'), True),)


class DownloadTask(TimestampedMixin, BaseModel):
    client = peewee.ForeignKeyField(
        DownloadClient,
        backref="download_tasks",
        on_delete="CASCADE",
        column_name="client_id",
    )
    # 影片番号不是 provider 身份，只是宿主业务投影，允许任务早于影片入库。
    movie = peewee.CharField(max_length=255, null=True, column_name="movie_number", index=True)
    remote_id = peewee.CharField(max_length=255)
    name = peewee.CharField(max_length=255)
    state = peewee.CharField(max_length=32, default="queued", index=True)
    progress = peewee.FloatField(default=0)
    # completed_source_ref 的结构由同 bundle 的 storage provider 定义。
    completed_source_ref = JsonTextField(null=True, default=None)
    # 导入是宿主自己的业务流程，不能与 provider 的远端状态混用。
    import_status = peewee.CharField(max_length=32, default="pending", index=True)
    import_task_run = peewee.ForeignKeyField(
        BackgroundTaskRun,
        null=True,
        backref="download_import_tasks",
        on_delete="SET NULL",
        column_name="import_task_run_id",
    )

    class Meta:
        table_name = "download_task"
        indexes = ((('client', 'remote_id'), True),)
