import peewee
from peewee import SQL

from src.model.base import BaseModel, JsonbField
from src.model.catalog.movies import Movie
from src.model.mixins import TimestampedMixin


class RatingDimension(TimestampedMixin, BaseModel):
    """用户自定义评分维度；单账号系统，全库共用一套。"""

    name = peewee.CharField(max_length=64, unique=True)
    min_value = peewee.IntegerField(default=1)
    max_value = peewee.IntegerField(default=5)
    # 前端暂不开放权重编辑；总分已按权重计算。
    weight = peewee.FloatField(default=1.0)

    class Meta:
        table_name = "rating_dimension"


class MovieRating(TimestampedMixin, BaseModel):
    """单部影片的个人评分；values 以维度 id 字符串为 key 存分值。"""

    movie = peewee.ForeignKeyField(
        Movie,
        unique=True,
        backref="rating",
        on_delete="CASCADE",
        verbose_name="影片",
    )
    values = JsonbField(
        null=False,
        default=dict,
        constraints=[SQL("DEFAULT '{}'::jsonb")],
        verbose_name="各维度分值",
    )
    total_score = peewee.FloatField(null=False, default=0.0, verbose_name="总分")

    class Meta:
        table_name = "movie_rating"
