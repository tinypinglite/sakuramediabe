from datetime import datetime

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel
from src.schema.common.clip_collections import ClipCollectionSummary


class MediaClipCreateRequest(SchemaModel):
    # 用户从同一媒体资源选两张缩略图圈出区间，首尾顺序不限，service 内部取 min/max。
    start_thumbnail_id: int = Field(gt=0)
    end_thumbnail_id: int = Field(gt=0)
    title: str = ""

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return value.strip()


class MediaClipUpdateRequest(SchemaModel):
    title: str

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return value.strip()


class MediaClipResource(SchemaModel):
    clip_id: int
    media_id: int | None
    movie_number: str | None
    start_offset_seconds: int
    end_offset_seconds: int
    title: str
    duration_seconds: int
    file_size_bytes: int
    # 封面按区间首帧缩略图实时解析；来源媒体已删除时为空。
    cover_image: ImageResource | None = None
    stream_url: str
    created_at: datetime


class MediaClipDetailResource(MediaClipResource):
    # 区间内所有缩略图，供前端循环播放成动态预览。
    preview_frames: list[ImageResource] = []
    # 该片段所属的合集，供前端「加入合集」选择器回显已勾选项。
    collections: list[ClipCollectionSummary] = []


class MediaClipThumbnailResource(SchemaModel):
    clip_id: int
    # 源媒体缩略图 id，与 /media/{id}/thumbnails 保持一致语义。
    thumbnail_id: int
    # 片段自身时间轴的相对秒数 = 源缩略图 offset - 片段 start_offset_seconds，供进度条定位跳转。
    offset_seconds: int
    image: ImageResource
