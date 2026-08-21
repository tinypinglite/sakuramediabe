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
    # Torznab 搜索接口地址；鉴权 key 可选，随每个索引器独立配置（为空则不携带 apikey 参数）。
    url = peewee.CharField(max_length=1024)
    # 索引器种类（IndexerKind）：pt / bt；PT 禁绑 cloud115 下载入口。
    kind = peewee.CharField(max_length=32)
    # 每个索引器自己的 Torznab 鉴权 key，可空；为空时搜索请求不带 apikey。
    api_key = peewee.CharField(max_length=255, null=True)

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
    # qB 采样快照：保留原始状态与传输指标，列表读取无需每次反查下载器。
    raw_state = peewee.CharField(
        max_length=32,
        default="",
        constraints=[peewee.SQL("DEFAULT ''")],
    )
    download_speed_bytes = peewee.BigIntegerField(
        default=0,
        constraints=[peewee.SQL("DEFAULT 0")],
    )
    uploaded_speed_bytes = peewee.BigIntegerField(
        default=0,
        constraints=[peewee.SQL("DEFAULT 0")],
    )
    downloaded_bytes = peewee.BigIntegerField(
        default=0,
        constraints=[peewee.SQL("DEFAULT 0")],
    )
    total_size_bytes = peewee.BigIntegerField(
        default=0,
        constraints=[peewee.SQL("DEFAULT 0")],
    )
    eta_seconds = peewee.IntegerField(null=True)
    progress_synced_at = peewee.DateTimeField(null=True)
    import_status = peewee.CharField(max_length=32, default="pending", index=True)
    # TaskRun 是下载后入库的唯一执行记录；终态历史被清理时仅断开关联。
    import_task_run = peewee.ForeignKeyField(
        BackgroundTaskRun,
        null=True,
        backref="download_import_tasks",
        on_delete="SET NULL",
        column_name="import_task_run_id",
    )
    # 进入活跃下载态（stalled/downloading）的时刻，由 qB 对账维护：排队/暂停/完成/做种时清空。
    # 停滞/慢速清理按它计时——qB 接口没有"开始下载时刻"字段（只有 added_on 与 last_activity），
    # 直接用 added_on 会把排队时长算进去，2000 部订阅 + 10 并发的排队场景会误删队尾种子。
    download_started_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "download_task"
        indexes = ((("client", "info_hash"), True),)
