from datetime import datetime

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel
from src.schema.playback.clips import MediaClipResource


class ClipCollectionCreateRequest(SchemaModel):
    name: str = Field(min_length=1)
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return value


class ClipCollectionUpdateRequest(SchemaModel):
    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return value


class ClipCollectionResource(SchemaModel):
    id: int
    name: str
    description: str = ""
    clip_count: int = 0
    # 合集封面取第一个片段的封面；空合集或来源缺失时为空。
    cover_image: ImageResource | None = None
    # 合集 HLS 清单签名 URL（12 小时有效），供前端 media_kit 一次性加载全集；空合集为空。
    playlist_url: str | None = None
    created_at: datetime
    updated_at: datetime


class ClipCollectionClipItemResource(MediaClipResource):
    position: int


class ClipCollectionSetClipsRequest(SchemaModel):
    # 幂等地把合集成员设置为这个有序列表，既覆盖重排也覆盖批量设置成员。
    clip_ids: list[int]


class ClipCollectionThumbnailResource(SchemaModel):
    collection_id: int
    # 来源切片，便于前端联动定位（点击缩略图回跳到对应片段）。
    clip_id: int
    # 来源 MediaThumbnail.id。
    thumbnail_id: int
    # 合集时间轴上的秒数 = 前序所有切片 duration 之和 + 该缩略图在片段内的相对秒数。
    offset_seconds: int
    image: ImageResource
    # 宽高沿用所属源媒体分辨率（同 MediaThumbnailResource 语义），未探测出时为 None。
    width: int | None = None
    height: int | None = None
