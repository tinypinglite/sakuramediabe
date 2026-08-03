"""跨服务共享的查询与验证工具函数。"""

from collections.abc import Sequence

from peewee import Model, ModelSelect

from src.api.exception.errors import ApiError


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


def validate_page(page: int, page_size: int, *, error_code: str) -> None:
    """校验分页参数。"""
    if page <= 0:
        raise ApiError(422, error_code, "page must be greater than 0", {"page": page})
    if page_size <= 0 or page_size > 100:
        raise ApiError(
            422, error_code, "page_size must be between 1 and 100", {"page_size": page_size}
        )


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
