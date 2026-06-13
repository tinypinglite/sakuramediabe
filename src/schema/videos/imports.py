from typing import List

from pydantic import Field, field_validator

from src.schema.common.base import SchemaModel


class VideoImportRequest(SchemaModel):
    # 指定一个目录或单个视频文件导入；可选地一并关联合集。
    source_path: str = Field(min_length=1)
    library_id: int | None = None
    collection_id: int | None = None

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source_path cannot be blank")
        return normalized


class VideoImportResultResource(SchemaModel):
    created_count: int = 0
    skipped_count: int = 0
    video_item_ids: List[int] = Field(default_factory=list)
