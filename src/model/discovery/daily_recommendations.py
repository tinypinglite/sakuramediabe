import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.catalog.movies import Movie
from src.model.mixins import TimestampedMixin


class DailyRecommendationItem(TimestampedMixin, BaseModel):
    snapshot_date = peewee.DateField(index=True)
    movie = peewee.ForeignKeyField(
        Movie,
        backref="daily_recommendation_items",
        on_delete="CASCADE",
        unique=True,
    )
    rank = peewee.IntegerField(unique=True)
    score = peewee.FloatField(default=0.0)
    reason_codes = JsonTextField(default=list)
    reason_texts = JsonTextField(default=list)
    signal_scores = JsonTextField(default=dict)
    generated_at = peewee.DateTimeField(index=True)

    class Meta:
        table_name = "daily_recommendation_item"
