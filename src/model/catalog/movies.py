import peewee
from peewee import SQL

from src.model.base import BaseModel, CaseSensitiveCharField, JsonbField, JsonTextField
from src.model.catalog.actors import Actor
from src.model.catalog.images import Image
from src.model.catalog.tags import Tag
from src.model.mixins import TimestampedMixin

# 受保护字段白名单（v2-lite 字段主权）：插件可写字段的宿主固定名单。
# 当前开放 title / summary / maker_name / director_name / is_collection（宿主严格刷新已收敛走
# gateway，插件可补充/修正文案、厂商/导演信息和合集判定）；后续每个可写字段必须由第一个
# 真实插件提出、补 MOVIE_FIELD_CODECS 类型校验、并收敛对应宿主写点后才加入。
# 白名单非空后，已持久化 Movie 的裸 save() 会被护栏拒绝。
PROTECTED_MOVIE_FIELDS: frozenset[str] = frozenset(
    {"title", "summary", "maker_name", "director_name", "is_collection"}
)

# 受保护字段 codec：字段名 -> 接受的值类型；开放写入前必须补上（v2-lite 文档约定）。
MOVIE_FIELD_CODECS: dict[str, type] = {
    "title": str,
    "summary": str,
    "maker_name": str,
    "director_name": str,
    "is_collection": bool,
}


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
    # 互动数刷新节奏属于影片领域；不再借通用资源任务投影保存成功时间。
    interaction_synced_at = peewee.DateTimeField(null=True, index=True)
    heat = peewee.IntegerField(null=False, default=0)
    is_collection = peewee.BooleanField(null=False, default=False, index=True)
    is_subscribed = peewee.BooleanField(null=False, default=False, index=True)
    is_blacklisted = peewee.BooleanField(null=False, default=False, index=True)
    subscribed_at = peewee.DateTimeField(null=True, index=True)
    # 订阅资源查询是影片领域状态：保留可见进度与重试预算，不再依赖通用资源任务台账。
    subscription_search_state = peewee.CharField(
        max_length=32, default="pending", index=True
    )
    subscription_search_attempt_count = peewee.IntegerField(default=0)
    subscription_search_retry_round = peewee.IntegerField(default=0)
    subscription_search_last_attempted_at = peewee.DateTimeField(null=True)
    subscription_search_last_succeeded_at = peewee.DateTimeField(null=True)
    subscription_search_next_retry_at = peewee.DateTimeField(null=True)
    subscription_search_error_code = peewee.CharField(max_length=64, null=True)
    subscription_search_last_error = peewee.TextField(null=True)
    subscription_search_last_error_at = peewee.DateTimeField(null=True)
    extra = JsonTextField(null=True, default=None, verbose_name="额外元数据")
    # v2-lite 字段主权：字段 -> owner 映射（缺键代表自动宿主管理，host:manual 代表人工）；
    # mutation_revision 只表示受保护字段及其 owner 的版本，不是整条 Movie 的全局版本。
    # constraints 里的服务端 DEFAULT 与 v0.5.0 收敛迁移对齐，保证新库（initdb 渲染）
    # 与 v0.4.21 存量库（迁移 ALTER）schema 同构，裸 INSERT 也有兜底。
    field_owners = JsonbField(
        null=False,
        default=dict,
        constraints=[SQL("DEFAULT '{}'::jsonb")],
        verbose_name="受保护字段 owner 映射",
    )
    mutation_revision = peewee.BigIntegerField(
        null=False,
        default=0,
        constraints=[SQL("DEFAULT 0")],
        verbose_name="受保护字段版本",
    )

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
        # movie_number 存 provider（JavDB）给出的规范原样，只去首尾空白、不做任何归一化改写：
        # 分隔符与大小写都是有效信息（一本道 072625_001 与加勒比 072625-001 是两部不同影片，
        # 东热 n0646 的规范写法就是小写）。系统内部各处番号副本列只允许拷贝本列；
        # 人工输入的匹配统一走 service_helpers.find_movie_by_number（大小写不敏感 + 分隔符候选）。
        self.javdb_id = (self.javdb_id or "").strip()
        self.movie_number = (self.movie_number or "").strip()
        self._guard_protected_field_write(
            only=kwargs.get("only"),
            force_insert=kwargs.get("force_insert", False),
        )
        return super().save(*args, **kwargs)

    @classmethod
    def update(cls, *args, **kwargs):
        # 与 save(only=...) 同级的运行时护栏：批量 UPDATE 同样禁止写受保护字段。
        # peewee 的 __data dict 与 kwargs 会合并，两侧都要检查，杜绝混合调用绕过。
        written_fields: set[str] = set()
        if args and isinstance(args[0], dict):
            written_fields.update(
                key.name if isinstance(key, peewee.Field) else key
                for key in args[0]
            )
        written_fields.update(kwargs)
        protected = PROTECTED_MOVIE_FIELDS & written_fields
        if protected:
            raise RuntimeError(
                f"受保护字段禁止直接 UPDATE: {sorted(protected)}"
            )
        return super().update(*args, **kwargs)

    def _guard_protected_field_write(self, *, only, force_insert: bool) -> None:
        # 护栏只约束已持久化行的写路径：新建 INSERT 由 DB 服务端默认值兜底，不受此限。
        if force_insert or self._pk is None or not PROTECTED_MOVIE_FIELDS:
            return
        if not only:
            # 未传 only 的 save() 会把 _data 全列写回，可能覆盖插件刚写入的受保护字段，
            # 因此一旦开放任何字段就必须显式窄更新。
            raise RuntimeError(
                f"已持久化 Movie 的 save() 必须传 only（受保护字段: {sorted(PROTECTED_MOVIE_FIELDS)}）"
            )
        written_field_names: set[str] = set()
        for field in only:
            if isinstance(field, peewee.Field):
                written_field_names.add(field.name)
            elif isinstance(field, str):
                # peewee 的 save(only=...) 接受字段名字符串，同样纳入护栏。
                written_field_names.add(field)
        protected = PROTECTED_MOVIE_FIELDS & written_field_names
        if protected:
            raise RuntimeError(
                f"受保护字段禁止直接写入: {sorted(protected)}"
            )

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
        constraints = [
            SQL(
                "CONSTRAINT movie_subscription_blacklist_exclusive "
                "CHECK (NOT (is_subscribed AND is_blacklisted))"
            )
        ]


# 人工输入按番号点查统一走 UPPER(movie_number) 等值匹配（movie_number_match_expression），
# 函数索引保证它不退化为顺扫；索引名保持与历史 schema 兼容。
Movie.add_index(
    peewee.ModelIndex(
        Movie,
        (peewee.fn.UPPER(Movie.movie_number),),
        name="movie_movie_number_upper",
    )
)

# 影片卡片列表排序用的复合索引：nullable 排序字段（release_date / subscribed_at）经
# build_ordered_expressions 渲染为 ``DESC NULLS LAST``，索引与排序表达式同向，planner 直接
# Index Scan 取页内行，避免全表扫 + 全量排序；反向扫描同时服务 asc 方向。
# 索引名保持与历史 schema 兼容，避免存量库重复创建同义索引。
Movie.add_index(
    peewee.ModelIndex(
        Movie,
        (
            peewee.Ordering(Movie.release_date, "DESC", nulls="last"),
            peewee.Ordering(Movie.id, "DESC", nulls="last"),
        ),
        name="movie_release_date_sort",
    )
)
Movie.add_index(
    peewee.ModelIndex(
        Movie,
        (
            peewee.Ordering(Movie.subscribed_at, "DESC", nulls="last"),
            peewee.Ordering(Movie.id, "DESC", nulls="last"),
        ),
        name="movie_subscribed_at_sort",
    )
)


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
    IMAGE_SEARCH_INDEX_STATUS_PENDING = 0
    IMAGE_SEARCH_INDEX_STATUS_FAILED = 1
    IMAGE_SEARCH_INDEX_STATUS_SUCCESS = 2

    movie = peewee.ForeignKeyField(Movie, backref="plot_image_links", on_delete="CASCADE")
    image = peewee.ForeignKeyField(Image, backref="movie_plot_links", on_delete="CASCADE")
    image_search_index_status = peewee.IntegerField(default=IMAGE_SEARCH_INDEX_STATUS_PENDING)

    class Meta:
        table_name = "movie_plot_image"
        indexes = (
            (("movie", "image"), True),
            (("image_search_index_status", "id"), False),
        )


class Subtitle(TimestampedMixin, BaseModel):
    movie = peewee.ForeignKeyField(Movie, backref="subtitle_items", on_delete="CASCADE")
    file_path = peewee.CharField(max_length=1024)

    class Meta:
        table_name = "subtitle"
        indexes = ((("movie", "file_path"), True),)
