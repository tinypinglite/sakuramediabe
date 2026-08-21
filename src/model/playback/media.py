import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.catalog.images import Image
from src.model.catalog.movies import Movie
from src.model.mixins import TimestampedMixin
from src.model.playback.libraries import MediaLibrary
from src.model.videos.items import VideoItem


class Media(TimestampedMixin, BaseModel):
    # 缩略图状态属于 Media 本身：MediaThumbnail 是成功产物，不能承担失败、退避和人工重试状态。
    THUMBNAIL_STATE_PENDING = "pending"
    THUMBNAIL_STATE_RETRY_WAIT = "retry_wait"
    THUMBNAIL_STATE_TERMINAL = "terminal"
    THUMBNAIL_STATE_SUCCEEDED = "succeeded"

    # 解耦后一条 Media 归属 movie（JAV）或 video_item（非 JAV）之一，由 service 层保证恰好其一。
    movie = peewee.ForeignKeyField(
        Movie,
        field=Movie.movie_number,
        null=True,
        backref="media_items",
        on_delete="CASCADE",
        column_name="movie_number",
    )
    video_item = peewee.ForeignKeyField(
        VideoItem,
        null=True,
        backref="media_items",
        on_delete="CASCADE",
        column_name="video_item_id",
    )
    library = peewee.ForeignKeyField(
        MediaLibrary,
        null=True,
        backref="media_items",
        on_delete="SET NULL",
        column_name="library_id",
    )
    # 本地库文件绝对路径（本地专用）；cloud115 等云盘 backend 走 backend_locator。
    # unique=True 对 NULL 不生效（PostgreSQL NULL != NULL），本地库仍保持路径唯一。
    path = peewee.CharField(max_length=1024, null=True, unique=True)
    # backend 相关的定位符（cloud115: {fid, pickcode, name, source_path}；未来 smb 等各自形状）。
    # locator dict 必须由 service 层统一构造保证键序固定，JsonTextField 序列化才稳定，
    # (library, backend_locator) 复合唯一索引（见 Meta.indexes）据此防同库重复登记。
    backend_locator = JsonTextField(null=True, default=None)
    storage_mode = peewee.CharField(max_length=32, null=True)
    resolution = peewee.CharField(max_length=32, null=True)
    # 本地库为 fingerprint-v1 抽样 sha256 裸 hex；cloud115 库存 "sha1:<hex>" 带前缀形式，
    # 两者值域天然不相交，判重查询仍须显式限定目标库范围。
    content_fingerprint = peewee.CharField(max_length=255, null=True, index=True)
    file_size_bytes = peewee.BigIntegerField(default=0)
    duration_seconds = peewee.IntegerField(default=0)
    # 统一存放整理后的探测结果，避免把 codec/profile/bitrate 拆成多列重复维护。
    video_info = JsonTextField(null=True)
    special_tags = peewee.CharField(max_length=255, default="普通")
    valid = peewee.BooleanField(default=True)
    # 失败次数与退避信息落在媒体领域，避免通用任务台账被移除后又失去收口语义。
    thumbnail_generation_state = peewee.CharField(
        max_length=32,
        default=THUMBNAIL_STATE_PENDING,
    )
    thumbnail_attempt_count = peewee.IntegerField(default=0)
    thumbnail_deferred_count = peewee.IntegerField(default=0)
    thumbnail_next_retry_at = peewee.DateTimeField(null=True)
    thumbnail_last_error_code = peewee.CharField(max_length=64, null=True)
    thumbnail_last_error = peewee.TextField(null=True)
    thumbnail_terminal_at = peewee.DateTimeField(null=True)
    # 记录本次状态对应的内容版本；内容变化或文件恢复后可重新打开终态失败。
    thumbnail_source_fingerprint = peewee.CharField(max_length=255, null=True)

    @property
    def display_path(self) -> str:
        """返回可用于 API/日志展示的非空存储位置，不把云端 locator 冒充本地路径。"""
        if self.path:
            return self.path
        locator = self.backend_locator or {}
        backend = self.library.backend if self.library_id is not None else "remote"
        label = locator.get("name") or locator.get("pickcode") or locator.get("fid") or str(self.id)
        return f"{backend}:{label}"

    def save(self, *args, **kwargs):
        # 恰好其一不变量：一条 Media 必须归属 movie(JAV) 或 video_item(非 JAV) 之一，
        # 不能两者都空或都非空。读外键原始列值判断，不触发关联加载。
        if (self.movie_number is None) == (self.video_item_id is None):
            raise ValueError("Media must belong to exactly one of movie / video_item")
        # 存储定位不变量：至少一个非空。本地库用 path，cloud115 等云盘 backend 用 backend_locator。
        if self.path is None and self.backend_locator is None:
            raise ValueError("Media must have either path or backend_locator")
        return super().save(*args, **kwargs)

    class Meta:
        table_name = "media"
        # 同库同 locator 只允许登记一条；NULL 不参与唯一约束，本地行（locator=NULL）不受影响。
        indexes = (
            (("library", "backend_locator"), True),
            (("thumbnail_generation_state", "thumbnail_next_retry_at"), False),
        )


class MediaThumbnail(TimestampedMixin, BaseModel):
    JOYTAG_INDEX_STATUS_PENDING = 0
    JOYTAG_INDEX_STATUS_FAILED = 1
    JOYTAG_INDEX_STATUS_SUCCESS = 2
    # 非 JAV 媒体的缩略图不参与图像检索向量索引，落明确终态避免长期滞留 PENDING。
    JOYTAG_INDEX_STATUS_SKIPPED = 3

    media = peewee.ForeignKeyField(Media, backref="thumbnails", on_delete="CASCADE")
    image = peewee.ForeignKeyField(Image, backref="media_thumbnails", on_delete="CASCADE")
    offset = peewee.IntegerField(index=True)
    joytag_index_status = peewee.IntegerField(default=JOYTAG_INDEX_STATUS_PENDING, index=True)

    class Meta:
        table_name = "media_thumbnail"
        indexes = ((("media", "offset"), True),)


class MediaProgress(TimestampedMixin, BaseModel):
    media = peewee.ForeignKeyField(Media, backref="progress_items", on_delete="CASCADE")
    position_seconds = peewee.IntegerField(default=0)
    last_watched_at = peewee.DateTimeField(null=True)

    class Meta:
        table_name = "media_progress"
        indexes = ((("media",), True),)


class MediaPoint(TimestampedMixin, BaseModel):
    media = peewee.ForeignKeyField(Media, backref="points", on_delete="CASCADE")
    thumbnail = peewee.ForeignKeyField(
        MediaThumbnail,
        backref="points",
        on_delete="CASCADE",
    )
    offset_seconds = peewee.IntegerField(index=True)

    class Meta:
        table_name = "media_point"


class MediaClip(TimestampedMixin, BaseModel):
    # 片段是独立资产：来源 Media 被删除时只置空引用，片段记录与其物理文件都保留。
    media = peewee.ForeignKeyField(Media, null=True, backref="clips", on_delete="SET NULL")
    # 快照来源番号，便于来源 Media/Movie 删除后片段仍可归属与展示。
    movie_number = peewee.CharField(max_length=64, null=True, index=True)
    start_offset_seconds = peewee.IntegerField(index=True)
    end_offset_seconds = peewee.IntegerField(index=True)
    title = peewee.CharField(max_length=255, default="")
    # 片段产物 mp4 相对 media_clip_root_path 的路径，独立目录便于单独挂卷。
    file_path = peewee.CharField(max_length=1024)
    file_size_bytes = peewee.BigIntegerField(default=0)
    duration_seconds = peewee.IntegerField(default=0)

    class Meta:
        table_name = "media_clip"
        # 幂等去重仅在来源 media 存活期间有效：media 删除后被 SET NULL，
        # NULL 不参与唯一约束，但此时已无入口对孤立片段再建同区间，故不影响实际。
        indexes = ((("media", "start_offset_seconds", "end_offset_seconds"), True),)
