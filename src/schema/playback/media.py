from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel


class HlsStreamResource(SchemaModel):
    quality: str
    resolution: str
    bandwidth: int
    url: str


class HlsStreamListResponse(SchemaModel):
    streams: list[HlsStreamResource]


class MediaPointKind(str, Enum):
    # 时刻归属过滤：JAV 仅影片媒体、VIDEO 仅非 JAV 视频媒体、ALL 不限。
    JAV = "jav"
    VIDEO = "video"
    ALL = "all"


class MediaProgressUpdateRequest(SchemaModel):
    position_seconds: int = Field(ge=0)

    @field_validator("position_seconds")
    @classmethod
    def validate_position_seconds(cls, value: int) -> int:
        if value < 0:
            raise ValueError("position_seconds cannot be negative")
        return value


class MediaProgressResource(SchemaModel):
    media_id: int
    last_position_seconds: int
    last_watched_at: datetime


class MediaPointCreateRequest(SchemaModel):
    thumbnail_id: int = Field(gt=0)

    @field_validator("thumbnail_id")
    @classmethod
    def validate_thumbnail_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("thumbnail_id must be greater than 0")
        return value


class MediaPointResource(SchemaModel):
    point_id: int
    media_id: int
    thumbnail_id: int
    offset_seconds: int
    image: ImageResource
    created_at: datetime


class MediaPointListItemResource(SchemaModel):
    point_id: int
    media_id: int
    # 非 JAV 媒体没有番号，改为可空并附带 video_item_id 供前端区分归属。
    movie_number: str | None = None
    video_item_id: int | None = None
    thumbnail_id: int
    offset_seconds: int
    image: ImageResource
    created_at: datetime


class MediaThumbnailResource(SchemaModel):
    thumbnail_id: int
    media_id: int
    offset_seconds: int
    image: ImageResource
    # 尺寸来自实际落盘 WebP；同一媒体的一组缩略图共享同一 HLS 清晰度或本地视频流尺寸。
    width: int | None = None
    height: int | None = None


class InvalidMediaResource(SchemaModel):
    id: int
    # 非 JAV 媒体无番号，番号可空，标题回退到 VideoItem.title。
    movie_number: str | None = None
    video_item_id: int | None = None
    movie_title: str | None = None
    cover_image: ImageResource | None = None
    thin_cover_image: ImageResource | None = None
    path: str
    library_id: int | None
    library_name: str | None
    file_size_bytes: int
    updated_at: datetime


class MediaListItemResource(SchemaModel):
    id: int
    # 归属判别：jav 关联 movie_number，video 关联 video_item_id，二者互斥。
    kind: Literal["jav", "video"]
    movie_number: str | None = None
    video_item_id: int | None = None
    title: str | None = None
    cover_image: ImageResource | None = None
    thin_cover_image: ImageResource | None = None
    library_id: int | None = None
    library_name: str | None = None
    path: str
    file_size_bytes: int
    duration_seconds: int
    resolution: str | None = None
    special_tags: str
    valid: bool
    # 仅 JAV 媒体有意义，非 JAV 视频恒为 None。
    heat: int | None = None
    created_at: datetime
    updated_at: datetime


class MediaValidityCheckResponse(SchemaModel):
    id: int
    path: str
    file_exists: bool
    valid_before: bool
    valid_after: bool
    updated: bool
    invalidated: bool
    revived: bool
    checked_at: datetime
