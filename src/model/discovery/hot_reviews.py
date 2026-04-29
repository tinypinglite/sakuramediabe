import peewee

from src.model.base import BaseModel
from src.model.catalog.movies import Movie
from src.model.mixins import TimestampedMixin


class HotReviewItem(TimestampedMixin, BaseModel):
    source_key = peewee.CharField(max_length=64, index=True)
    period = peewee.CharField(max_length=32, index=True)
    rank = peewee.IntegerField(index=True)
    review_id = peewee.IntegerField(index=True)
    movie_number = peewee.CharField(max_length=255, index=True)
    movie = peewee.ForeignKeyField(Movie, backref="hot_review_items", on_delete="CASCADE")
    score = peewee.IntegerField(default=0)
    content = peewee.TextField(default="")
    review_created_at = peewee.CharField(max_length=64, null=True)
    username = peewee.CharField(max_length=255, default="")
    like_count = peewee.IntegerField(default=0)
    watch_count = peewee.IntegerField(default=0)

    class Meta:
        table_name = "hot_review_item"
        indexes = (
            (("source_key", "period", "rank"), True),
            (("source_key", "period"), False),
        )
