
from peewee import JOIN, fn

from src.api.exception.errors import ApiError
from src.model import MovieTag, Tag
from src.schema.catalog.movies import (
    MovieCollectionType,
    MovieListItemResource,
    MovieListStatus,
    MovieSpecialTagFilter,
    TagListItemResource,
)
from src.schema.common.pagination import PageResponse
from src.common.service_helpers import build_ordered_expressions, resolve_sort_expression
from src.service.catalog.movie_service import MovieService


class TagService:
    @classmethod
    def _normalize_query(cls, query: str | None) -> str | None:
        if query is None:
            return None
        normalized = query.strip()
        if not normalized:
            raise ApiError(422, "invalid_tag_filter", "Invalid tag filter", {"query": query})
        return normalized

    @classmethod
    def _build_tag_sort(cls, sort: str | None):
        def _movie_count_order(field_name: str, direction: str) -> list:
            # 影片数相同时按名称稳定排序，保证标签筛选器展示顺序可预期。
            return build_ordered_expressions(
                fn.COUNT(MovieTag.movie),
                direction,
                tie_breaker=Tag.id,
                extra=(Tag.name.asc(),),
            )

        # 空白 sort 归一化为默认值（旧实现 ``(sort or "movie_count:desc").strip()`` 语义）。
        normalized_sort = (sort or "").strip() or "movie_count:desc"
        return resolve_sort_expression(
            normalized_sort,
            {"movie_count": fn.COUNT(MovieTag.movie), "name": Tag.name},
            error_code="invalid_tag_filter",
            tie_breaker=Tag.id,
            extra_sort_builders={"movie_count": _movie_count_order},
        )

    @classmethod
    def _tag_count_query(cls, query: str | None = None):
        movie_count = fn.COUNT(MovieTag.movie).alias("movie_count")
        tag_query = (
            Tag.select(Tag, movie_count)
            .join(MovieTag, JOIN.LEFT_OUTER)
            .group_by(Tag.id)
        )
        normalized_query = cls._normalize_query(query)
        if normalized_query is not None:
            tag_query = tag_query.where(Tag.name.contains(normalized_query))
        return tag_query

    @classmethod
    def list_tags(cls, query: str | None = None, sort: str | None = None) -> list[TagListItemResource]:
        tags = list(cls._tag_count_query(query).order_by(*cls._build_tag_sort(sort)))
        return TagListItemResource.from_items(tags)

    @classmethod
    def get_tag(cls, tag_id: int) -> TagListItemResource:
        tag = cls._tag_count_query().where(Tag.id == tag_id).first()
        if tag is None:
            raise ApiError(404, "tag_not_found", "Tag not found", {"tag_id": tag_id})
        return TagListItemResource.from_attributes_model(tag)

    @classmethod
    def list_tag_movies(
        cls,
        tag_id: int,
        year: int | None = None,
        status: MovieListStatus = MovieListStatus.ALL,
        collection_type: MovieCollectionType = MovieCollectionType.ALL,
        special_tag: MovieSpecialTagFilter | None = None,
        sort: str | None = None,
        director_name: str | None = None,
        maker_name: str | None = None,
        heat_min: int | None = None,
        heat_max: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MovieListItemResource]:
        cls.get_tag(tag_id)
        return MovieService.list_movies(
            tag_ids=[tag_id],
            year=year,
            status=status,
            collection_type=collection_type,
            special_tag=special_tag,
            sort=sort,
            director_name=director_name,
            maker_name=maker_name,
            heat_min=heat_min,
            heat_max=heat_max,
            page=page,
            page_size=page_size,
        )
