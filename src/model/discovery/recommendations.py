import peewee

from src.model.base import BaseModel
from src.model.catalog.movies import Movie
from src.model.mixins import TimestampedMixin


class MovieSimilarity(TimestampedMixin, BaseModel):
    source_movie = peewee.ForeignKeyField(
        Movie, backref="similarity_sources", on_delete="CASCADE"
    )
    target_movie = peewee.ForeignKeyField(
        Movie, backref="similarity_targets", on_delete="CASCADE"
    )
    score = peewee.FloatField(default=0.0)
    rank = peewee.IntegerField()

    class Meta:
        table_name = "movie_similarity"
        indexes = (
            (("source_movie", "target_movie"), True),
            (("source_movie", "rank"), False),
        )
