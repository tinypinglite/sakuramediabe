from src.common import normalize_movie_number
from src.config.config import settings
from src.model import Movie
from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway


class MovieCollectionService:
    @staticmethod
    def _normalized_collection_prefixes() -> list[str]:
        # 配置侧 validator 已把前缀规范成去空白+大写，这里直接用，不要再套番号归一化。
        return [prefix for prefix in settings.media.others_number_features if prefix]

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
    def matches_configured_collection(cls, movie_number: str) -> bool:
        # 自动规则 = 番号特征前缀匹配（已去掉时长阈值判定）。
        return cls._matches_collection_prefix(movie_number)

    @classmethod
    def sync_movie_collections(cls) -> dict[str, int]:
        movies = list(
            Movie.select(
                Movie.id,
                Movie.movie_number,
                Movie.is_collection,
                Movie.field_owners,
            ).order_by(Movie.id)
        )
        to_collection_ids: list[int] = []
        to_single_ids: list[int] = []
        matched_count = 0

        for movie in movies:
            target_is_collection = cls.matches_configured_collection(movie.movie_number)
            if target_is_collection:
                matched_count += 1
            # 自动规则只管理没有 owner 的字段；插件和人工都由同一映射表达。
            if (movie.field_owners or {}).get("is_collection"):
                continue
            if bool(movie.is_collection) == target_is_collection:
                continue
            if target_is_collection:
                to_collection_ids.append(movie.id)
            else:
                to_single_ids.append(movie.id)

        for movie_id in to_collection_ids:
            MovieOwnershipGateway.update_host_unowned(
                movie_id,
                {"is_collection": True},
            )
        for movie_id in to_single_ids:
            MovieOwnershipGateway.update_host_unowned(
                movie_id,
                {"is_collection": False},
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
