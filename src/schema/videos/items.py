from datetime import datetime
from typing import List

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import MovieMediaResource
from src.schema.common.base import SchemaModel


class VideoCollectionRef(SchemaModel):
    # 视频所属合集的精简引用，仅用于列表/详情返回，避免循环导入完整 VideoCollectionResource。
    id: int
    name: str


class VideoItemListItemResource(SchemaModel):
    id: int
    title: str
    summary: str = ""
    cover_image: ImageResource | None = None
    release_date: datetime | None = None
    # 时长 / 文件大小取该条目第一条媒体（Media.id 最小），无媒体时为 0。
    duration_seconds: int = 0
    file_size_bytes: int = 0
    # 封面像素宽高 = 第一条媒体的探测分辨率（来源：Media.resolution，"WxH" 字符串拆分）。
    # 用于前端瀑布流按真实比例排版；探测失败或无媒体时为 None，前端回退 16:9 占位。
    cover_width: int | None = None
    cover_height: int | None = None
    media_count: int = 0
    can_play: bool = False
    # 该视频归属的全部合集（0..N），按合集名称升序。列表/详情共享同一字段。
    collections: List[VideoCollectionRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class VideoItemDetailResource(VideoItemListItemResource):
    media_items: List[MovieMediaResource] = Field(default_factory=list)


class VideoItemCreateRequest(SchemaModel):
    title: str = Field(min_length=1)
    summary: str = ""
    release_date: datetime | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title cannot be blank")
        return normalized


class VideoItemUpdateRequest(SchemaModel):
    title: str | None = None
    summary: str | None = None
    release_date: datetime | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("title cannot be blank")
        return normalized
