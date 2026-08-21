"""手动字幕导入请求。"""

from pydantic import field_validator

from src.schema.common.base import SchemaModel


class SubtitleImportCreateRequest(SchemaModel):
    source_path: str

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("source_path cannot be blank")
        return normalized
