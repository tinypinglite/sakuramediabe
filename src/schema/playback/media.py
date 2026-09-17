from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel
from src.schema.videos.items import VideoCollectionRef


class MediaPointKind(str, Enum):
    # 时刻归属过滤：JAV 仅影片媒体、VIDEO 仅非 JAV 视频媒体、ALL 不限。
    JAV = "jav"
    VIDEO = "video"
    ALL = "all"


class MediaThumbnailGenerationState(str, Enum):
    # 由 Media 持久化：成功产物仍在 MediaThumbnail，状态用于列表筛选与运维处置。
    PENDING = "pending"
    RETRY_WAIT = "retry_wait"
    TERMINAL = "terminal"
    SUCCEEDED = "succeeded"


class MediaThumbnailResetRequest(SchemaModel):
    media_ids: list[int] = Field(min_length=1, max_length=1000)

    @field_validator("media_ids", mode="before")
    @classmethod
    def reject_boolean_media_ids(cls, value):
        if isinstance(value, (list, tuple)) and any(
            isinstance(item, bool) for item in value
        ):
            raise ValueError("media_ids 必须全部为正整数")
        return value

    @field_validator("media_ids")
    @classmethod
    def validate_media_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("media_ids 必须全部为正整数")
        if len(set(value)) != len(value):
            raise ValueError("media_ids 不可重复")
        return value


class MediaThumbnailResetResponse(SchemaModel):
    reset_count: int


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


class MediaPlaybackModeResource(SchemaModel):
    mode: Literal["direct", "proxy"] | None


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
    media_id: int | None
    thumbnail_id: int | None
    offset_seconds: int
    image: ImageResource
    created_at: datetime


class MediaPointListItemResource(SchemaModel):
    point_id: int
    media_id: int | None
    # 非 JAV 媒体没有番号，改为可空并附带 video_item_id 供前端区分归属。
    movie_number: str | None = None
    video_item_id: int | None = None
    thumbnail_id: int | None
    offset_seconds: int
    image: ImageResource
    created_at: datetime


class MediaThumbnailResource(SchemaModel):
    thumbnail_id: int
    media_id: int
    offset_seconds: int
    image: ImageResource
    # 尺寸来自缩略图产物；同一媒体的一组缩略图共享同一视频流尺寸。
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
    file_name: str
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
    file_name: str
    file_size_bytes: int
    duration_seconds: int
    resolution: str | None = None
    valid: bool
    thumbnail_generation_state: MediaThumbnailGenerationState
    thumbnail_last_error_code: str | None = None
    # 仅 JAV 媒体有意义，非 JAV 视频恒为 None。
    heat: int | None = None
    created_at: datetime
    updated_at: datetime


class DuplicateMediaListItemResource(MediaListItemResource):
    # 仅 PornBox 媒体有归属合集；JAV 媒体返回空列表。
    collections: list[VideoCollectionRef] = Field(default_factory=list)


class DuplicateMediaGroupResource(SchemaModel):
    kind: Literal["jav", "video"]
    media_count: int
    media_items: list[DuplicateMediaListItemResource]


class MultiVersionMovieResource(SchemaModel):
    movie_number: str
    media_count: int
    media_items: list[MediaListItemResource]
