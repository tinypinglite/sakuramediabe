import peewee

from src.model.base import BaseModel
from src.model.catalog.movies import Movie
from src.model.mixins import TimestampedMixin


class RankingItem(TimestampedMixin, BaseModel):
    source_key = peewee.CharField(max_length=64, index=True)
    board_key = peewee.CharField(max_length=64, index=True)
    period = peewee.CharField(max_length=32, default="", index=True)
    rank = peewee.IntegerField(index=True)
    movie_number = peewee.CharField(max_length=255, index=True)
    movie = peewee.ForeignKeyField(Movie, backref="ranking_items", on_delete="CASCADE")

    class Meta:
        table_name = "ranking_item"
        indexes = (
            (("source_key", "board_key", "period", "rank"), True),
            (("source_key", "board_key", "period"), False),
        )
