import peewee

from src.model.base import BaseModel, CaseSensitiveCharField, JsonTextField
from src.model.catalog.actors import Actor
from src.model.catalog.images import Image
from src.model.catalog.tags import Tag
from src.model.mixins import TimestampedMixin


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
    series_name = peewee.CharField(max_length=255, verbose_name="系列名称", null=True, index=True)
    want_watch_count = peewee.IntegerField(default=0)
    comment_count = peewee.IntegerField(default=0)
    heat = peewee.IntegerField(null=False, default=0)
    is_collection = peewee.BooleanField(null=False, default=False, index=True)
    is_subscribed = peewee.BooleanField(null=False, default=False, index=True)
    subscribed_at = peewee.DateTimeField(null=True, index=True)
    extra = JsonTextField(null=True, default=None, verbose_name="额外元数据")

    def save(self, *args, **kwargs):
        self.javdb_id = (self.javdb_id or "").strip()
        self.movie_number = (self.movie_number or "").strip()
        return super().save(*args, **kwargs)

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
