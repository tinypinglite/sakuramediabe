"""跨服务共享的查询与验证工具函数。"""

import asyncio
import random
from collections.abc import Callable, Sequence
from typing import Any

from peewee import Model, ModelSelect, Ordering, fn

from src.api.exception.errors import ApiError


def rest_between_requests(min_seconds: float, max_seconds: float) -> float:
    """取一个降频休息用的随机延迟（秒）；sleep 由调用方在日志/进度事件之后执行。"""
    return random.uniform(min_seconds, max_seconds)


async def rest_between_requests_async(min_seconds: float, max_seconds: float) -> float:
    """取一个降频休息用的随机延迟（秒）的异步版；sleep 由调用方执行。"""
    return random.uniform(min_seconds, max_seconds)


def unlink_ignore_missing(path) -> None:
    """删除文件；文件本就不存在时静默放行（竞态下先删后查的常见场景）。"""
    try:
        path.unlink()
    except FileNotFoundError:
        return


def safe_int(value: Any, default: int | None = 0) -> int | None:
    """宽容转 int：None / 非数值返回 default，不做任何截断。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def backoff_delay(attempt: int, *, step: float = 0.5, cap: float = 2.0) -> float:
    """线性退避延迟：``min(step * attempt, cap)`` 秒；``attempt`` 从 1 起。"""
    return min(step * max(attempt, 0), cap)


async def poll_until(
    delays: Sequence[float],
    check,
    *,
    on_failure: Callable[[Exception], None] | None = None,
    retry_exceptions: tuple[type[BaseException], ...] | None = None,
) -> None:
    """按固定延迟列表轮询直到 ``check()``（可等待）不再抛异常；全部失败时抛最后一次异常。

    ``delays`` 首项可为 0（立即首次尝试）；``on_failure`` 记录每次失败信息供最终错误组装；
    ``retry_exceptions`` 为只重试的异常类型白名单（其余异常立即透传，不消耗延迟轮询）。
    """
    last_exc: Exception | None = None
    for delay in delays:
        if delay:
            await asyncio.sleep(delay)
        try:
            await check()
            return
        except Exception as exc:
            if retry_exceptions is not None and not isinstance(exc, retry_exceptions):
                raise
            last_exc = exc
            if on_failure is not None:
                on_failure(exc)
    assert last_exc is not None  # delays 至少一项
    raise last_exc


def count_by_owner(
    link_model: type[Model],
    owner_fk,
    owner_ids: Sequence,
    *extra_filters,
) -> dict[int, int]:
    """按 owner 外键批量统计关联条数，返回 {owner_id: count}。

    ``link_model`` 为关联表模型，``owner_fk`` 为其 owner 外键列；
    ``extra_filters`` 为可选的附加过滤条件（如已判死的下载任务）。
    """
    if not owner_ids:
        return {}
    query = (
        link_model.select(owner_fk, fn.COUNT(link_model.id))
        .where(owner_fk.in_(owner_ids))
        .group_by(owner_fk)
    )
    if extra_filters:
        query = query.where(*extra_filters)
    return {row[0]: int(row[1]) for row in query.tuples()}


def require_record(
    model_class: type[Model],
    *conditions,
    error_code: str,
    error_message: str,
    error_details: dict | None = None,
    status_code: int = 404,
    query: ModelSelect | None = None,
):
    """从数据库查询单条记录，不存在则抛出 ApiError。

    ``query`` 为自定义查询（如包含 JOIN），传入后 ``model_class`` 仅用于类型标注。
    """
    if query is not None:
        record = query.where(*conditions).get_or_none()
    else:
        record = model_class.get_or_none(*conditions)
    if record is None:
        raise ApiError(status_code, error_code, error_message, error_details)
    return record


def require_by_id(
    model_class: type[Model],
    entity_id: int,
    entity_name: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    error_details_key: str | None = None,
    query: ModelSelect | None = None,
):
    """按主键查找单条记录，不存在抛 ApiError(404)。

    ``entity_name`` 用于生成默认错误码/文案/详情键（如 "actor" -> actor_not_found /
    actor_id）；需要定制时显式传 error_code / error_message / error_details_key。
    """
    details_key = error_details_key or f"{entity_name}_id"
    return require_record(
        model_class,
        model_class.id == entity_id,
        error_code=error_code or f"{entity_name}_not_found",
        error_message=error_message or f"{entity_name} not found",
        error_details={details_key: entity_id},
        query=query,
    )


def validate_page(page: int, page_size: int, *, error_code: str) -> None:
    """校验分页参数。"""
    if page <= 0:
        raise ApiError(422, error_code, "page must be greater than 0", {"page": page})
    if page_size <= 0 or page_size > 100:
        raise ApiError(
            422, error_code, "page_size must be between 1 and 100", {"page_size": page_size}
        )


def paginate(
    query: ModelSelect,
    page: int,
    page_size: int,
    *,
    error_code: str,
    item_mapper=None,
    response_model,
):
    """执行分页查询并组装 PageResponse；内部无条件校验分页参数。

    ``item_mapper`` 为单行资源转换（缺省原样返回模型行），``response_model`` 为
    ``PageResponse[X]`` 参数化后的响应类。
    """
    validate_page(page, page_size, error_code=error_code)
    total = query.count()
    start = (page - 1) * page_size
    rows = list(query.offset(start).limit(page_size))
    items = rows if item_mapper is None else [item_mapper(row) for row in rows]
    return response_model(items=items, page=page, page_size=page_size, total=total)


def emit_progress(callback, **payload) -> None:
    """向进度回调发射事件；回调为空时静默跳过。"""
    if callback is not None:
        callback(payload)


def resolve_sort(
    value: str | None,
    allowed_sorts: dict[str, Sequence],
    *,
    default_key: str,
    error_code: str,
) -> Sequence:
    """通过 dict-lookup 解析排序表达式，无效值抛 ApiError(422)。"""
    if value is None:
        return allowed_sorts[default_key]
    normalized = value.strip().lower()
    if not normalized:
        return allowed_sorts[default_key]
    if normalized not in allowed_sorts:
        raise ApiError(422, error_code, "Invalid sort expression", {"sort": value})
    return allowed_sorts[normalized]


def build_ordered_expressions(
    sort_field,
    direction: str,
    *,
    nullable: bool = False,
    tie_breaker=None,
    extra: Sequence = (),
) -> list:
    """把排序列 + 方向组装成 order_by 表达式列表。

    - ``nullable`` 为 True 时对排序列与稳定次级排序显式追加 ``NULLS LAST`` 把空值垫后：
      PostgreSQL 下渲染原生 NULLS 语法，排序可被同向复合索引直接服务；旧实现用
      ``is_null()`` 垫后表达式，会把可索引排序降级为全表扫 + 全量排序（30w 行影片列表
      实测 200ms+，同量级下命中索引的写法约 4ms）；
    - ``tie_breaker`` 追加稳定的次级排序（nullable 时同样带 NULLS LAST）；
    - ``extra`` 在 tie_breaker 之前追加的额外排序（如 tag 的 name 次级）。

    排序方向统一用 ``Ordering(sort_field, direction.upper())`` 表达：对普通 Field 与关联
    子查询（ModelSelect）都兼容（子查询没有 ``.asc()/.desc()`` 方法）。
    """
    direction = direction.upper()
    ordered_field = Ordering(sort_field, direction, nulls="last" if nullable else None)
    tie = (
        []
        if tie_breaker is None
        else [Ordering(tie_breaker, direction, nulls="last" if nullable else None)]
    )
    return [ordered_field, *extra, *tie]


def resolve_sort_expression(
    value: str | None,
    field_map: dict[str, Any],
    *,
    error_code: str,
    nullable_fields: set[str] = frozenset(),
    tie_breaker=None,
    default: Sequence | None = None,
    extra_sort_builders: dict[str, Callable[[str, str], list]] | None = None,
) -> Sequence | None:
    """解析 ``field:direction`` 排序表达式并补稳定次级排序；无效值抛 ApiError(422)。

    - ``value`` 为 None 或空串时返回 ``default``（None 表示调用方自行决定是否排序）；
    - ``field_map`` 为字段名 -> 排序列的映射；
    - ``nullable_fields`` 中的字段加 ``is_null()`` 垫后；
    - ``tie_breaker`` 追加稳定次级排序；
    - ``extra_sort_builders`` 为字段特有的排序构造器（子查询 / 聚合列），签名
      ``(field_name, direction) -> [expressions]``，命中时覆盖通用组装。
    """
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    try:
        field_name, direction = normalized.split(":", 1)
    except ValueError as exc:
        raise ApiError(422, error_code, "Invalid sort expression", {"sort": value}) from exc
    if field_name not in field_map or direction not in ("asc", "desc"):
        raise ApiError(422, error_code, "Invalid sort expression", {"sort": value})
    if extra_sort_builders and field_name in extra_sort_builders:
        return extra_sort_builders[field_name](field_name, direction)
    return build_ordered_expressions(
        field_map[field_name],
        direction,
        nullable=field_name in nullable_fields,
        tie_breaker=tie_breaker,
    )


def playable_exists_expression():
    """返回"影片是否存在可播放媒体"的子查询表达式。"""
    from peewee import fn

    from src.model import Media, Movie

    playable_media = Media.select(Media.id).where(
        Media.valid == True,
        Media.movie == Movie.movie_number,
    )
    return fn.EXISTS(playable_media)


def media_exists_expression():
    """返回"影片是否存在任何 media 行"的子查询表达式（不区分 valid）。"""
    from peewee import fn

    from src.model import Media, Movie

    media_query = Media.select(Media.id).where(Media.movie == Movie.movie_number)
    return fn.EXISTS(media_query)


def find_movie_by_number(value: str):
    """人工输入定位影片：逐个候选做大小写不敏感的等值点查，命中即返回。

    列里存 provider 规范原样，输入的大小写/分隔符未必一致：大小写用 UPPER(movie_number)
    抹平（走函数索引 movie_movie_number_upper），分隔符靠候选集依次尝试 ``_``/``-`` 互换。
    候选有序、先精确后互换——两种分隔符的影片同时存在时（一本道/加勒比同日番号），
    必须命中用户输入的那一部，绝不能落到互换形态的另一部上。

    系统内部两列规范值之间的比较（如 DownloadTask/Media 与 Movie 的 JOIN）不要用它，
    直接裸列相等即可。
    """
    from peewee import fn

    from src.common.movie_numbers import movie_number_lookup_values
    from src.model import Movie

    for candidate in movie_number_lookup_values(value):
        movie = Movie.get_or_none(fn.UPPER(Movie.movie_number) == candidate)
        if movie is not None:
            return movie
    return None


def media_special_tag_match_expression(media_tag: str):
    """按空格分隔标签做精确匹配，避免把普通子串误判成命中。"""
    from src.model import Media

    return (
        (Media.special_tags == media_tag)
        | Media.special_tags.startswith(f"{media_tag} ")
        | Media.special_tags.endswith(f" {media_tag}")
        | Media.special_tags.contains(f" {media_tag} ")
    )


def parse_special_tags_text(value: str | None) -> list[str]:
    """将空格分隔的标签文本解析为列表。"""
    if value is None:
        return []
    return [part.strip() for part in value.split() if part.strip()]


def with_movie_card_relations(query):
    """给影片卡片查询追加封面、竖封面和系列关联。"""
    from peewee import JOIN

    from src.model import Image, Movie, MovieSeries

    thin_cover_alias = Image.alias()
    # 影片卡片响应统一依赖这三类关联，集中维护避免多个 service 重复拼 join。
    query = (
        query.select_extend(Image, thin_cover_alias, MovieSeries)
        .join(Image, JOIN.LEFT_OUTER, on=(Movie.cover_image == Image.id))
        .switch(Movie)
        .join(
            thin_cover_alias,
            JOIN.LEFT_OUTER,
            on=(Movie.thin_cover_image == thin_cover_alias.id),
            attr="thin_cover_image",
        )
        .switch(Movie)
        .join(MovieSeries, JOIN.LEFT_OUTER, on=(Movie.series == MovieSeries.id))
    )
    return query, thin_cover_alias
