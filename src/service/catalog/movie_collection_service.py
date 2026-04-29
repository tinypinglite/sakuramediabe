from src.common import normalize_movie_number
from src.config.config import settings
from src.model import Movie


class MovieCollectionService:
    @staticmethod
    def _normalized_collection_prefixes() -> list[str]:
        prefixes = []
        for prefix in settings.media.others_number_features:
            normalized_prefix = normalize_movie_number(prefix)
            if normalized_prefix:
                prefixes.append(normalized_prefix)
        return prefixes

    @classmethod
    def _matches_collection_prefix(cls, movie_number: str) -> bool:
        normalized_movie_number = normalize_movie_number(movie_number)
        if not normalized_movie_number:
            return False
        for prefix in cls._normalized_collection_prefixes():
            if normalized_movie_number.startswith(prefix):
                return True
        return False

    @classmethod
    def _matches_collection_duration(cls, duration_minutes: int | None) -> bool:
        normalized_duration = int(duration_minutes or 0)
        if normalized_duration <= 0:
            return False
        return normalized_duration > settings.media.collection_duration_threshold_minutes

    @classmethod
    def matches_configured_collection(
        cls,
        movie_number: str,
        duration_minutes: int | None = 0,
    ) -> bool:
        # 自动规则 = 番号特征 + 时长阈值，任一命中都视为合集影片。
        return (
            cls._matches_collection_prefix(movie_number)
            or cls._matches_collection_duration(duration_minutes)
        )

    @classmethod
    def sync_movie_collections(cls) -> dict[str, int]:
        movies = list(
            Movie.select(
                Movie.id,
                Movie.movie_number,
                Movie.duration_minutes,
                Movie.is_collection,
                Movie.is_collection_overridden,
            ).order_by(Movie.id)
        )
        to_collection_ids: list[int] = []
        to_single_ids: list[int] = []
        matched_count = 0

        for movie in movies:
            target_is_collection = cls.matches_configured_collection(
                movie.movie_number,
                movie.duration_minutes,
            )
            if target_is_collection:
                matched_count += 1
            # 手动覆盖优先：被手动标记过的影片不再参与自动规则同步改写。
            if bool(movie.is_collection_overridden):
                continue
            if bool(movie.is_collection) == target_is_collection:
                continue
            if target_is_collection:
                to_collection_ids.append(movie.id)
            else:
                to_single_ids.append(movie.id)

        if to_collection_ids:
            (
                Movie.update(is_collection=True)
                .where(Movie.id.in_(to_collection_ids))
                .execute()
            )
        if to_single_ids:
            (
                Movie.update(is_collection=False)
                .where(Movie.id.in_(to_single_ids))
                .execute()
            )

        total_movies = len(movies)
        updated_to_collection_count = len(to_collection_ids)
        updated_to_single_count = len(to_single_ids)
        return {
            "total_movies": total_movies,
            "matched_count": matched_count,
            "updated_to_collection_count": updated_to_collection_count,
            "updated_to_single_count": updated_to_single_count,
            "unchanged_count": total_movies - updated_to_collection_count - updated_to_single_count,
        }
