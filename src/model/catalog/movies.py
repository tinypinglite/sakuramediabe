import peewee

from src.model.base import BaseModel, CaseSensitiveCharField, JsonTextField
from src.model.catalog.actors import Actor
from src.model.catalog.images import Image
from src.model.catalog.tags import Tag
from src.model.mixins import TimestampedMixin


class MovieSeries(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True, verbose_name="系列名称")

    def save(self, *args, **kwargs):
        # 系列名称统一去除首尾空白，避免同一系列产生重复实体。
        self.name = (self.name or "").strip()
        return super().save(*args, **kwargs)

    class Meta:
        table_name = "movie_series"


class Movie(TimestampedMixin, BaseModel):
    javdb_id = CaseSensitiveCharField(max_length=64, unique=True, index=True, verbose_name="JavDB ID")
    movie_number = peewee.CharField(max_length=255, unique=True, index=True, verbose_name="番号")
    title = peewee.TextField(verbose_name="标题")
    release_date = peewee.DateTimeField(verbose_name="发布时间", index=True, null=True)
    duration_minutes = peewee.IntegerField(verbose_name="时长", default=0, index=True)
    score = peewee.FloatField(verbose_name="评分", index=True, default=0)
    score_number = peewee.IntegerField(verbose_name="评分人数", default=0)
    watched_count = peewee.IntegerField(default=0)
    cover_image = peewee.ForeignKeyField(
        Image,
        null=True,
        backref="movies_as_cover",
        on_delete="SET NULL",
        verbose_name="封面图片",
    )
    thin_cover_image = peewee.ForeignKeyField(
        Image,
        null=True,
        backref="movies_as_thin_cover",
        on_delete="SET NULL",
        verbose_name="竖封面图片",
    )
    summary = peewee.TextField(verbose_name="描述", default="")
    series = peewee.ForeignKeyField(
        MovieSeries,
        null=True,
        backref="movies",
        on_delete="SET NULL",
        verbose_name="系列",
    )
    maker_name = peewee.CharField(max_length=255, verbose_name="厂商名称", null=True)
    director_name = peewee.CharField(max_length=255, verbose_name="导演名称", null=True)
    want_watch_count = peewee.IntegerField(default=0)
    comment_count = peewee.IntegerField(default=0)
    heat = peewee.IntegerField(null=False, default=0)
    is_collection = peewee.BooleanField(null=False, default=False, index=True)
    is_collection_overridden = peewee.BooleanField(null=False, default=False, index=True)
    is_subscribed = peewee.BooleanField(null=False, default=False, index=True)
    subscribed_at = peewee.DateTimeField(null=True, index=True)
    desc = peewee.TextField(verbose_name="DMM描述", default="")
    desc_zh = peewee.TextField(verbose_name="中文描述", default="")
    title_zh = peewee.TextField(verbose_name="中文标题", default="")
    extra = JsonTextField(null=True, default=None, verbose_name="额外元数据")

    @staticmethod
    def resolve_series(series_name: str | None) -> MovieSeries | None:
        normalized_name = (series_name or "").strip()
        if not normalized_name:
            return None
        # 影片只保存系列外键，名称来源统一汇聚到 MovieSeries 表。
        series, _ = MovieSeries.get_or_create(name=normalized_name)
        return series

    @classmethod
    def create(cls, **query):
        if "series_name" in query:
            query["series"] = cls.resolve_series(query.pop("series_name"))
        return super().create(**query)

    def save(self, *args, **kwargs):
        self.javdb_id = (self.javdb_id or "").strip()
        self.movie_number = (self.movie_number or "").strip()
        return super().save(*args, **kwargs)

    @property
    def series_name(self) -> str | None:
        if self.series_id is None:
            return None
        try:
            series = self.series
        except MovieSeries.DoesNotExist:
            return None
        return series.name if series is not None else None

    @property
    def cover_url(self) -> str | None:
        if self.cover_image_id and self.cover_image:
            return self.cover_image.medium
        return None

    class Meta:
        table_name = "movie"


class MovieActor(BaseModel):
    movie = peewee.ForeignKeyField(Movie, backref="movie_actor_links", on_delete="CASCADE")
    actor = peewee.ForeignKeyField(Actor, backref="movie_actor_links", on_delete="CASCADE")

    class Meta:
        table_name = "movie_actor"
        indexes = ((("movie", "actor"), True),)


class MovieTag(BaseModel):
    movie = peewee.ForeignKeyField(Movie, backref="movie_tag_links", on_delete="CASCADE")
    tag = peewee.ForeignKeyField(Tag, backref="movie_tag_links", on_delete="CASCADE")

    class Meta:
        table_name = "movie_tag"
        indexes = ((("movie", "tag"), True),)


class MoviePlotImage(BaseModel):
    movie = peewee.ForeignKeyField(Movie, backref="plot_image_links", on_delete="CASCADE")
    image = peewee.ForeignKeyField(Image, backref="movie_plot_links", on_delete="CASCADE")

    class Meta:
        table_name = "movie_plot_image"
        indexes = ((("movie", "image"), True),)


class Subtitle(TimestampedMixin, BaseModel):
    movie = peewee.ForeignKeyField(Movie, backref="subtitle_items", on_delete="CASCADE")
    file_path = peewee.CharField(max_length=1024)

    class Meta:
        table_name = "subtitle"
        indexes = ((("movie", "file_path"), True),)
