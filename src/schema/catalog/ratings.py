from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from src.schema.common.base import SchemaModel


class RatingDimensionResource(SchemaModel):
    id: int
    name: str
    min_value: int
    max_value: int
    weight: float
    has_scores: bool = False


class RatingDimensionUpsertRequest(SchemaModel):
    name: str = Field(min_length=1, max_length=64)
    # 只能选择固定档位；min_value 始终为 1。更新时省略表示保留当前范围。
    max_value: Literal[5, 10, 100] | None = None
    # 前端暂不开放权重编辑；更新时省略表示保留原值，创建时默认 1.0。
    weight: float | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized


class MovieRatingValueResource(SchemaModel):
    dimension_id: int
    name: str
    value: int
    min_value: int
    max_value: int


class MovieRatingResource(SchemaModel):
    movie_number: str
    rated: bool
    total_score: float | None = None
    values: list[MovieRatingValueResource] = Field(default_factory=list)
    updated_at: datetime | None = None


class MovieRatingUpsertRequest(SchemaModel):
    # 维度 id -> 分值；未提交的维度视为未填，不参与总分。
    values: dict[int, int] = Field(min_length=1)
