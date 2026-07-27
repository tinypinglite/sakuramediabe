import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.enums import DownloadClientKind
from src.model.mixins import TimestampedMixin
from src.model.playback.libraries import MediaLibrary
from src.model.system.activity import BackgroundTaskRun


class DownloadClient(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    # 下载入口种类（DownloadClientKind）：所有按下载器分派的逻辑以本列为权威判定。
    kind = peewee.CharField(max_length=32, default=DownloadClientKind.QBITTORRENT.value, index=True)
    # 以下为 qbittorrent 专属连接字段；cloud115 kind 不使用（凭据在 media_library.backend_config）。
    base_url = peewee.CharField(max_length=255, null=True)
    username = peewee.CharField(max_length=255, null=True)
    password = peewee.CharField(max_length=255, null=True)
    client_save_path = peewee.CharField(max_length=1024, null=True)
    local_root_path = peewee.CharField(max_length=1024, null=True)
    media_library = peewee.ForeignKeyField(
        MediaLibrary,
        backref="download_clients",
        on_delete="CASCADE",
        column_name="media_library_id",
    )

    class Meta:
        table_name = "download_client"


# 115 凭据和下载根都属于媒体库，同一 115 库只能有一个离线下载入口；qB 不受此约束。
DownloadClient.add_index(
    peewee.ModelIndex(
        DownloadClient,
        (DownloadClient.media_library,),
        unique=True,
        name="download_client_cloud115_library_unique",
    ).where(DownloadClient.kind == DownloadClientKind.CLOUD115.value)
)


class Indexer(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    url = peewee.CharField(max_length=1024)
    kind = peewee.CharField(max_length=32)

    class Meta:
        table_name = "indexer"


class IndexerDownloadClient(TimestampedMixin, BaseModel):
    """索引器与下载器的多对多绑定：一个索引器可同时绑 qb 与 cloud115，提交时按全局偏好挑选。"""

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
        indexes = ((("indexer", "download_client"), True),)


class DownloadTask(TimestampedMixin, BaseModel):
    client = peewee.ForeignKeyField(
        DownloadClient,
        backref="download_tasks",
        on_delete="CASCADE",
        column_name="client_id",
    )
    # 番号。不是外键（下载任务可能先于影片入库、也可能压根解析不出番号）。取值约定：
    # 提交链路写入的是 Movie.movie_number 的规范原样（provider 形态）；qB 对账重建行时
    # 允许落 parse 猜测且只填空不覆写（见 DownloadSyncService）。与 Movie 的 JOIN 因此
    # 是两侧规范值的裸列精确比较，不要在查询里套 UPPER(TRIM())——会废掉本列索引（实测 1s -> 46s）。
    movie = peewee.CharField(max_length=255, null=True, column_name="movie_number", index=True)
    name = peewee.CharField(max_length=255)
    info_hash = peewee.CharField(max_length=128)
    # qb 任务为映射后的本地绝对路径；cloud115 任务为面包屑可读路径（仅展示用）。
    save_path = peewee.CharField(max_length=1024)
    # 后端结构化落地定位符：qb 为 NULL（save_path 即可定位），cloud115 存 {"cid": <hash 独立目录 cid>}。
    target_ref = JsonTextField(null=True, default=None)
    progress = peewee.FloatField(default=0)
    download_state = peewee.CharField(max_length=32, default="downloading", index=True)
    import_status = peewee.CharField(max_length=32, default="pending", index=True)

    class Meta:
        table_name = "download_task"
        indexes = ((("client", "info_hash"), True),)


class ImportJob(TimestampedMixin, BaseModel):
    # 本地导入 = 源目录绝对路径；cloud115 导入 = 源目录面包屑可读路径（仅展示用）。
    source_path = peewee.CharField(max_length=1024)
    # cloud115 导入的源目录 cid（重导据此重新枚举源）；本地导入为 NULL。
    source_cid = peewee.CharField(max_length=64, null=True)
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
    # 导入模式：auto（硬链接优先）/ cleanup-source（复制后删源）；失败文件重导时据此沿用原作业语义。
    transfer_mode = peewee.CharField(max_length=32, default="auto")
    imported_count = peewee.IntegerField(default=0)
    skipped_count = peewee.IntegerField(default=0)
    failed_count = peewee.IntegerField(default=0)
    failed_files = peewee.TextField(default="[]")
    started_at = peewee.DateTimeField(null=True)
    finished_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "import_job"
