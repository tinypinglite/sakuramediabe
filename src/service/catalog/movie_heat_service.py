from peewee import fn

from src.model import Movie
from src.model.base import get_database


class MovieHeatService:
    FORMULA_VERSION = "v6"

    # 参考值取当前业务库各互动字段的 P99，固定后避免全库分布变化导致历史热度漂移。
    WATCHED_COUNT_REFERENCE = 1308
    WANT_WATCH_COUNT_REFERENCE = 4991
    COMMENT_COUNT_REFERENCE = 41
    SCORE_NUMBER_REFERENCE = 6291
    HEAT_SCALE = 3_100

    @classmethod
    def build_heat_expression(cls):
        # 按固定 P99 参考值做线性累计，保留头部原始计数差异，不设置热度上限。
        normalized_heat = (
            (7.0 / 34.0) * Movie.watched_count.cast("REAL") / cls.WATCHED_COUNT_REFERENCE
            + (5.0 / 34.0) * Movie.want_watch_count.cast("REAL") / cls.WANT_WATCH_COUNT_REFERENCE
            + (17.0 / 34.0) * Movie.comment_count.cast("REAL") / cls.COMMENT_COUNT_REFERENCE
            + (5.0 / 34.0) * Movie.score_number.cast("REAL") / cls.SCORE_NUMBER_REFERENCE
        )
        # HEAT_SCALE 只是 P99 附近的展示基准，参考值以上继续线性增长。
        return fn.ROUND(normalized_heat * cls.HEAT_SCALE).cast("INTEGER")

    @classmethod
    def build_candidate_count_query(cls):
        computed_heat = cls.build_heat_expression()
        return Movie.select(fn.COUNT(Movie.id)).where(Movie.heat != computed_heat)

    @classmethod
    def build_update_query(cls):
        computed_heat = cls.build_heat_expression()
        return (
            Movie.update({Movie.heat: computed_heat})
            .where(Movie.heat != computed_heat)
        )

    @classmethod
    def build_single_movie_update_query(cls, movie_id: int):
        computed_heat = cls.build_heat_expression()
        return (
            Movie.update({Movie.heat: computed_heat})
            .where((Movie.id == movie_id) & (Movie.heat != computed_heat))
        )

    @classmethod
    def update_single_movie_heat(cls, movie_id: int) -> int:
        return cls.build_single_movie_update_query(movie_id).execute()

    @classmethod
    def update_movie_heat(cls) -> dict[str, int | str]:
        database = get_database()
        with database.atomic():
            candidate_count = cls.build_candidate_count_query().scalar() or 0
            updated_count = cls.build_update_query().execute()
        return {
            "candidate_count": candidate_count,
            "updated_count": updated_count,
            "formula_version": cls.FORMULA_VERSION,
        }
