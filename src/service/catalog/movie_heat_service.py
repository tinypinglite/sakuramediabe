from peewee import fn

from src.model import Movie
from src.model.base import get_database


class MovieHeatService:
    FORMULA_VERSION = "v2"

    # 参考值取当前业务库各互动字段的 P99，固定后避免全库分布变化导致历史热度漂移。
    WATCHED_COUNT_REFERENCE = 1308
    WANT_WATCH_COUNT_REFERENCE = 4991
    COMMENT_COUNT_REFERENCE = 41
    SCORE_NUMBER_REFERENCE = 6291
    HEAT_SCALE = 100

    @classmethod
    def build_heat_expression(cls):
        # 对数压缩累计计数，降低极端头部的碾压，同时保留计数越高热度越高的单调性。
        normalized_heat = (
            0.30
            * (
                fn.LN((Movie.watched_count + 1).cast("REAL"))
                / fn.LN(cls.WATCHED_COUNT_REFERENCE + 1)
            )
            + 0.30
            * (
                fn.LN((Movie.want_watch_count + 1).cast("REAL"))
                / fn.LN(cls.WANT_WATCH_COUNT_REFERENCE + 1)
            )
            + 0.10
            * (
                fn.LN((Movie.comment_count + 1).cast("REAL"))
                / fn.LN(cls.COMMENT_COUNT_REFERENCE + 1)
            )
            + 0.30
            * (
                fn.LN((Movie.score_number + 1).cast("REAL"))
                / fn.LN(cls.SCORE_NUMBER_REFERENCE + 1)
            )
        )
        # 不设置硬上限，保留参考值以上极端头部影片之间的排序差异。
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
