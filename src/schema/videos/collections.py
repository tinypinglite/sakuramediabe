from datetime import datetime
from typing import List

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel
from src.schema.videos.items import VideoItemListItemResource


class VideoCollectionResource(SchemaModel):
    id: int
    name: str
    description: str = ""
    item_count: int = 0
    # 合集封面取按顺序排在最前的视频封面；空合集或来源缺失时为空。
    cover_image: ImageResource | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_collection(
        cls, collection, item_count: int = 0, cover_image: ImageResource | None = None
    ) -> "VideoCollectionResource":
        return cls.from_peewee_model(
            collection, extra={"item_count": item_count, "cover_image": cover_image}
        )


class VideoCollectionItemResource(SchemaModel):
    # 合集成员，position 决定顺序播放次序。
    item_id: int
    position: int
    video: VideoItemListItemResource
    # 连播页所需「首个媒体」（Media.id 升序）的签名播放地址；仅在 include_play_url=True
    # 时填充，成员无媒体时为 None。供前端直接组装播放列表，免逐集拉详情（N+1）。
    play_url: str | None = None
    # 「首个媒体」（Media.id 升序）的 id，成员无媒体时为 None。供连播页右侧「整部合集」关键帧
    # 面板按此调 GET /media/{id}/thumbnails 拉该集关键帧；与 play_url 同源、恒返回（不依赖
    # include_play_url）。
    first_media_id: int | None = None


class VideoCollectionCreateRequest(SchemaModel):
    name: str = Field(min_length=1)
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized


class VideoCollectionUpdateRequest(SchemaModel):
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
        return normalized


class VideoCollectionItemAddRequest(SchemaModel):
    video_item_id: int = Field(gt=0)


class VideoCollectionReorderRequest(SchemaModel):
    # 按目标顺序给出合集成员 item_id 列表，service 据此重写 position。
    ordered_item_ids: List[int] = Field(min_length=1)
