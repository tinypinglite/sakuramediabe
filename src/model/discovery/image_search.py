import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.mixins import TimestampedMixin


class ImageSearchSession(TimestampedMixin, BaseModel):
    session_id = peewee.CharField(max_length=64, unique=True, index=True)
    status = peewee.CharField(max_length=32, default="ready")
    page_size = peewee.IntegerField(default=20)
    next_cursor = peewee.TextField(null=True)
    query_vector = JsonTextField(null=True)
    movie_ids = JsonTextField(null=True)
    exclude_movie_ids = JsonTextField(null=True)
    score_threshold = peewee.FloatField(null=True)
    expires_at = peewee.DateTimeField(index=True)

    class Meta:
        table_name = "image_search_session"
