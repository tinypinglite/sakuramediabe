import peewee

from src.model.base import BaseModel
from src.model.catalog.movies import Movie
from src.model.mixins import TimestampedMixin
from src.model.playback.media import Media, MediaPoint, MediaThumbnail


class MomentRecommendation(TimestampedMixin, BaseModel):
    rank = peewee.IntegerField(unique=True)
    score = peewee.FloatField(default=0.0)
    strategy = peewee.CharField(max_length=32, index=True)
    reason = peewee.CharField(max_length=255)
    movie = peewee.ForeignKeyField(Movie, backref="moment_recommendations", on_delete="CASCADE")
    media = peewee.ForeignKeyField(Media, backref="moment_recommendations", on_delete="CASCADE")
    thumbnail = peewee.ForeignKeyField(
        MediaThumbnail,
        backref="moment_recommendations",
        on_delete="CASCADE",
        unique=True,
    )
    offset_seconds = peewee.IntegerField(index=True)
    seed_point = peewee.ForeignKeyField(
        MediaPoint,
        null=True,
        backref="moment_recommendation_seeds",
        on_delete="SET NULL",
    )
    seed_thumbnail = peewee.ForeignKeyField(
        MediaThumbnail,
        null=True,
        backref="moment_recommendation_seed_thumbnails",
        on_delete="SET NULL",
    )
    source_movie = peewee.ForeignKeyField(
        Movie,
        null=True,
        backref="moment_recommendation_sources",
        on_delete="SET NULL",
    )
    visual_score = peewee.FloatField(null=True)
    movie_similarity_score = peewee.FloatField(null=True)
    generated_at = peewee.DateTimeField(index=True)

    class Meta:
        table_name = "moment_recommendation"
