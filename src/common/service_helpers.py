"""跨服务共享的查询与验证工具函数。"""

from typing import Optional, Sequence

from peewee import Model, ModelSelect

from src.api.exception.errors import ApiError


def require_record(
    model_class: type[Model],
    *conditions,
    error_code: str,
    error_message: str,
    error_details: Optional[dict] = None,
    status_code: int = 404,
    query: Optional[ModelSelect] = None,
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
    value: Optional[str],
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
    from src.model import Media, Movie

    from peewee import fn

    playable_media = Media.select(Media.id).where(
        Media.valid == True,
        Media.movie == Movie.movie_number,
    )
    return fn.EXISTS(playable_media)


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
