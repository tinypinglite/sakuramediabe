from typing import Generic, TypeVar

from pydantic import Field

from src.schema.common.base import SchemaModel

T = TypeVar("T")


class PageResponse(SchemaModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int = Field()
    total: int
