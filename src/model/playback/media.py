import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.catalog.images import Image
from src.model.catalog.movies import Movie
from src.model.mixins import TimestampedMixin
from src.model.playback.libraries import MediaLibrary


class Media(TimestampedMixin, BaseModel):
    movie = peewee.ForeignKeyField(
        Movie,
        field=Movie.movie_number,
        backref="media_items",
        on_delete="CASCADE",
        column_name="movie_number",
    )
    library = peewee.ForeignKeyField(
        MediaLibrary,
        null=True,
        backref="media_items",
        on_delete="SET NULL",
        column_name="library_id",
    )
    path = peewee.CharField(max_length=1024, unique=True)
    storage_mode = peewee.CharField(max_length=32, null=True)
    resolution = peewee.CharField(max_length=32, null=True)
    content_fingerprint = peewee.CharField(max_length=255, null=True, index=True)
    file_size_bytes = peewee.BigIntegerField(default=0)
    duration_seconds = peewee.IntegerField(default=0)
    # 统一存放整理后的探测结果，避免把 codec/profile/bitrate 拆成多列重复维护。
    video_info = JsonTextField(null=True)
    special_tags = peewee.CharField(max_length=255, default="普通")
    valid = peewee.BooleanField(default=True)

    class Meta:
        table_name = "media"


class MediaThumbnail(TimestampedMixin, BaseModel):
    JOYTAG_INDEX_STATUS_PENDING = 0
    JOYTAG_INDEX_STATUS_FAILED = 1
    JOYTAG_INDEX_STATUS_SUCCESS = 2

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
        # 同一来源媒体的同一区间只保留一条，创建时按此去重幂等。
        indexes = ((("media", "start_offset_seconds", "end_offset_seconds"), True),)
