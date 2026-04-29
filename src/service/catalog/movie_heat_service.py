from peewee import fn

from src.model import Movie
from src.model.base import get_database


class MovieHeatService:
    FORMULA_VERSION = "v1"

    @classmethod
    def build_heat_expression(cls):
        weighted_sum = (
            (Movie.want_watch_count * 7)
            + (Movie.comment_count * 7)
            + (Movie.score_number * 4)
        )
        raw_heat = weighted_sum.cast("REAL") / 20
        return fn.ROUND(raw_heat).cast("INTEGER")

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
