import math
from collections.abc import Iterable

from peewee import fn

from src.api.exception.errors import ApiError
from src.common.service_helpers import find_movie_by_number, require_record
from src.model import Movie, MovieRating, RatingDimension, get_database
from src.schema.catalog.ratings import (
    MovieRatingResource,
    MovieRatingUpsertRequest,
    MovieRatingValueResource,
    RatingDimensionResource,
    RatingDimensionUpsertRequest,
)

# 维度最低分固定 1；新维度默认 1-5，可选 1-10/1-100；影片总分固定 1-5。
MIN_SCORE = 1
DEFAULT_MAX_SCORE = 5
TOTAL_MAX_SCORE = 5


class RatingService:
    """个人评分的维度维护、影片评分写入与总分计算。

    评分值以 JSONB 存在 MovieRating.values，key 是维度 id 字符串。个人库规模有限，
    维度变更时直接整表重算总分，不额外维护派生表。
    """

    @classmethod
    def list_dimensions(cls) -> list[RatingDimensionResource]:
        dimensions = list(RatingDimension.select().order_by(RatingDimension.id))
        dimensions_with_scores = cls._dimension_ids_with_scores()
        return [
            cls._dimension_resource(
                dimension,
                has_scores=dimension.id in dimensions_with_scores,
            )
            for dimension in dimensions
        ]

    @classmethod
    def create_dimension(
        cls, payload: RatingDimensionUpsertRequest
    ) -> RatingDimensionResource:
        cls._ensure_name_available(payload.name)
        dimension = RatingDimension.create(
            name=payload.name,
            min_value=MIN_SCORE,
            max_value=payload.max_value or DEFAULT_MAX_SCORE,
            weight=payload.weight if payload.weight is not None else 1.0,
        )
        return cls._dimension_resource(dimension, has_scores=False)

    @classmethod
    def update_dimension(
        cls, dimension_id: int, payload: RatingDimensionUpsertRequest
    ) -> RatingDimensionResource:
        dimension = cls._require_dimension(dimension_id)
        cls._ensure_name_available(payload.name, exclude_id=dimension_id)
        # 纯改名不影响任何总分，避免无谓刷新全部评分的 updated_at；省略 weight 时保留原值。
        next_weight = dimension.weight if payload.weight is None else payload.weight
        next_max_value = (
            dimension.max_value if payload.max_value is None else payload.max_value
        )
        has_scores = cls._dimension_has_scores(dimension_id)
        if next_max_value != dimension.max_value and has_scores:
            raise ApiError(
                409,
                "rating_dimension_scale_locked",
                "该评分维度已有评分，不能修改分数范围",
                {
                    "dimension_id": dimension_id,
                    "max_value": dimension.max_value,
                },
            )
        weight_changed = next_weight != dimension.weight
        with get_database().atomic():
            dimension.name = payload.name
            dimension.min_value = MIN_SCORE
            dimension.max_value = next_max_value
            dimension.weight = next_weight
            dimension.save()
            if weight_changed:
                cls._recalculate_all()
        return cls._dimension_resource(dimension, has_scores=has_scores)

    @classmethod
    def delete_dimension(cls, dimension_id: int) -> None:
        dimension = cls._require_dimension(dimension_id)
        with get_database().atomic():
            # 评分值在 JSONB 里没有外键：删维度后统一重算，顺手清掉孤儿 key。
            dimension.delete_instance()
            cls._recalculate_all()

    @classmethod
    def get_rating(cls, movie_number: str) -> MovieRatingResource:
        movie = cls._require_movie(movie_number)
        rating = MovieRating.get_or_none(MovieRating.movie == movie)
        return cls._rating_resource(
            movie, rating, cls._dimensions_by_id()
        )

    @classmethod
    def save_rating(
        cls, movie_number: str, payload: MovieRatingUpsertRequest
    ) -> MovieRatingResource:
        movie = cls._require_movie(movie_number)
        dimensions = cls._dimensions_by_id()
        if not dimensions:
            raise ApiError(
                422,
                "rating_dimensions_empty",
                "请先设置评分维度",
            )
        normalized_values: dict[str, int] = {}
        for dimension_id, value in payload.values.items():
            dimension = dimensions.get(dimension_id)
            if dimension is None:
                raise ApiError(
                    422,
                    "rating_dimension_not_found",
                    "评分维度不存在",
                    {"dimension_id": dimension_id},
                )
            if value < dimension.min_value or value > dimension.max_value:
                raise ApiError(
                    422,
                    "rating_value_out_of_range",
                    "评分超出维度范围",
                    {
                        "dimension_id": dimension_id,
                        "min_value": dimension.min_value,
                        "max_value": dimension.max_value,
                        "value": value,
                    },
                )
            normalized_values[str(dimension_id)] = value
        total_score = cls._compute_total(dimensions, normalized_values)
        with get_database().atomic():
            rating = MovieRating.get_or_none(MovieRating.movie == movie)
            if rating is None:
                rating = MovieRating.create(
                    movie=movie,
                    values=normalized_values,
                    total_score=total_score,
                )
            else:
                rating.values = normalized_values
                rating.total_score = total_score
                rating.save(only=[MovieRating.values, MovieRating.total_score])
        return cls._rating_resource(movie, rating, dimensions)

    @classmethod
    def delete_rating(cls, movie_number: str) -> None:
        movie = cls._require_movie(movie_number)
        MovieRating.delete().where(MovieRating.movie == movie).execute()

    @classmethod
    def attach_movie_scores(
        cls, movies: Iterable[Movie], *, include_values: bool = False
    ) -> None:
        """给影片列表补 my_score；include_values 时附带维度分值摘要。"""
        movie_list = list(movies)
        if not movie_list:
            return
        rating_rows = list(
            MovieRating.select(
                MovieRating.movie, MovieRating.total_score, MovieRating.values
            )
            .where(MovieRating.movie.in_([movie.id for movie in movie_list]))
            .tuples()
        )
        scores = {movie_id: total_score for movie_id, total_score, _ in rating_rows}
        values_by_movie = (
            {
                movie_id: dict(values or {})
                for movie_id, _, values in rating_rows
            }
            if include_values
            else {}
        )
        for movie in movie_list:
            movie.my_score = scores.get(movie.id)
            if include_values:
                movie.my_rating_values = values_by_movie.get(movie.id, {})

    @staticmethod
    def rated_movie_ids():
        return MovieRating.select(MovieRating.movie)

    @staticmethod
    def dimension_score_expression(dimension_id: int):
        """维度分值排序列；调用方需先把 ``MovieRating`` LEFT JOIN 到影片查询。"""
        return fn.jsonb_extract_path_text(
            MovieRating.values, str(dimension_id)
        ).cast("double precision")

    @classmethod
    def _recalculate_all(cls) -> None:
        dimensions = cls._dimensions_by_id()
        for rating in list(MovieRating.select()):
            cls._store_rating_values(rating, dict(rating.values or {}), dimensions)

    @classmethod
    def _store_rating_values(
        cls,
        rating: MovieRating,
        values: dict,
        dimensions: dict[int, RatingDimension],
    ) -> None:
        """收窄到当前维度集合后重算总分；没有分值就删除整条评分。"""
        dimension_keys = {str(dimension.id) for dimension in dimensions.values()}
        effective: dict[str, int] = {}
        for raw_key, value in values.items():
            key = str(raw_key)
            if key in dimension_keys and isinstance(value, (int, float)):
                effective[key] = value
        if not effective:
            rating.delete_instance()
            return
        rating.values = effective
        rating.total_score = cls._compute_total(dimensions, effective)
        rating.save(only=[MovieRating.values, MovieRating.total_score])

    @staticmethod
    def _compute_total(
        dimensions: dict[int, RatingDimension], values: dict[str, int]
    ) -> float:
        weighted_sum = 0.0
        weight_sum = 0.0
        for dimension_id, dimension in dimensions.items():
            raw_value = values.get(str(dimension_id))
            if raw_value is None:
                continue
            span = dimension.max_value - dimension.min_value
            if span <= 0:
                continue
            ratio = (float(raw_value) - dimension.min_value) / span
            weighted_sum += dimension.weight * ratio
            weight_sum += dimension.weight
        if weight_sum <= 0:
            return 0.0
        total = (
            MIN_SCORE
            + (TOTAL_MAX_SCORE - MIN_SCORE) * weighted_sum / weight_sum
        )
        # 与前端预览的 half-up 取整保持一致，避免显示 3.3 保存后变 3.2。
        return math.floor(total * 10 + 0.5) / 10

    @classmethod
    def _dimension_resource(
        cls, dimension: RatingDimension, *, has_scores: bool
    ) -> RatingDimensionResource:
        return RatingDimensionResource(
            id=dimension.id,
            name=dimension.name,
            min_value=dimension.min_value,
            max_value=dimension.max_value,
            weight=dimension.weight,
            has_scores=has_scores,
        )

    @classmethod
    def _dimension_has_scores(cls, dimension_id: int) -> bool:
        return str(dimension_id) in cls._dimension_score_keys()

    @classmethod
    def _dimension_ids_with_scores(cls) -> set[int]:
        return {
            int(key) for key in cls._dimension_score_keys() if key.isdigit()
        }

    @staticmethod
    def _dimension_score_keys() -> set[str]:
        keys: set[str] = set()
        for (values,) in MovieRating.select(MovieRating.values).tuples():
            if isinstance(values, dict):
                keys.update(str(key) for key in values)
        return keys

    @classmethod
    def _dimensions_by_id(cls) -> dict[int, RatingDimension]:
        return {
            dimension.id: dimension
            for dimension in RatingDimension.select().order_by(RatingDimension.id)
        }

    @classmethod
    def _rating_resource(
        cls,
        movie: Movie,
        rating: MovieRating | None,
        dimensions: dict[int, RatingDimension],
    ) -> MovieRatingResource:
        if rating is None:
            return MovieRatingResource(movie_number=movie.movie_number, rated=False)
        stored_values = rating.values or {}
        values = [
            MovieRatingValueResource(
                dimension_id=dimension.id,
                name=dimension.name,
                value=int(stored_values[str(dimension.id)]),
                min_value=dimension.min_value,
                max_value=dimension.max_value,
            )
            for dimension in dimensions.values()
            if str(dimension.id) in stored_values
        ]
        return MovieRatingResource(
            movie_number=movie.movie_number,
            rated=True,
            total_score=rating.total_score,
            values=values,
            updated_at=rating.updated_at,
        )

    @staticmethod
    def _require_dimension(dimension_id: int) -> RatingDimension:
        return require_record(
            RatingDimension,
            RatingDimension.id == dimension_id,
            error_code="rating_dimension_not_found",
            error_message="评分维度不存在",
            error_details={"dimension_id": dimension_id},
        )

    @staticmethod
    def _require_movie(movie_number: str) -> Movie:
        movie = find_movie_by_number(movie_number)
        if movie is None:
            raise ApiError(
                404,
                "movie_not_found",
                "影片不存在",
                {"movie_number": movie_number},
            )
        return movie

    @staticmethod
    def _ensure_name_available(name: str, exclude_id: int | None = None) -> None:
        query = RatingDimension.select(RatingDimension.id).where(
            RatingDimension.name == name
        )
        if exclude_id is not None:
            query = query.where(RatingDimension.id != exclude_id)
        if query.exists():
            raise ApiError(
                409,
                "rating_dimension_exists",
                "评分维度名称已存在",
                {"name": name},
            )
