from datetime import datetime

from pydantic import Field

from src.schema.common.base import SchemaModel


class MovieSubtitleItemResource(SchemaModel):
    subtitle_id: int = Field(validation_alias="id")
    url: str
    created_at: datetime
    file_name: str


class MovieSubtitleListResource(SchemaModel):
    movie_number: str
    items: list[MovieSubtitleItemResource]
