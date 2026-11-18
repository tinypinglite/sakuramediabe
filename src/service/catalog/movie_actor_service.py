"""影片演员表的人工编辑。

用户第一次增删演员时，影片进入演员表人工接管状态（``field_owners.actors =
host:manual``），后续元数据刷新不再整表替换关联；恢复自动管理由 reset 端点负责。
"""

from src.api.exception.errors import ApiError
from src.common.service_helpers import find_movie_by_number
from src.model import Actor, Movie, MovieActor, get_database
from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway


class MovieActorService:
    """影片演员表的增删与接管状态维护。"""

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

    @classmethod
    def _locked_movie(cls, movie_number: str) -> Movie:
        """锁影片行，和元数据刷新的演员表替换串行化。"""
        movie = cls._require_movie(movie_number)
        locked = (
            Movie.select()
            .where(Movie.id == movie.id)
            .for_update()
            .get_or_none()
        )
        if locked is None:
            raise ApiError(
                404,
                "movie_not_found",
                "影片不存在",
                {"movie_number": movie_number},
            )
        return locked

    @staticmethod
    def _locked_canonical_actor(actor_id: int) -> Actor:
        """锁保留演员行；等锁期间若被合并，沿最新指针重新解析。"""
        seen_ids: set[int] = set()
        candidate_id = actor_id
        while candidate_id not in seen_ids:
            seen_ids.add(candidate_id)
            canonical = Actor.resolve_canonical(candidate_id)
            if canonical is None:
                break
            actor = (
                Actor.select()
                .where(Actor.id == canonical.id)
                .for_update()
                .get_or_none()
            )
            if actor is None:
                break
            if actor.merged_into_id is None:
                return actor
            candidate_id = actor.merged_into_id
        raise ApiError(404, "actor_not_found", "演员不存在", {"actor_id": actor_id})

    @classmethod
    def add_actor(cls, movie_number: str, actor_id: int) -> None:
        """幂等加入演员；已存在时只确保影片进入人工接管状态。"""
        with get_database().atomic():
            movie = cls._locked_movie(movie_number)
            actor = cls._locked_canonical_actor(actor_id)
            MovieActor.get_or_create(movie=movie, actor=actor)
            MovieOwnershipGateway.take_actor_list_ownership(movie.id)

    @classmethod
    def remove_actor(cls, movie_number: str, actor_id: int) -> None:
        """幂等移除演员；关联不存在时同样成功。"""
        with get_database().atomic():
            movie = cls._locked_movie(movie_number)
            actor = cls._locked_canonical_actor(actor_id)
            MovieActor.delete().where(
                MovieActor.movie == movie,
                MovieActor.actor == actor,
            ).execute()
            MovieOwnershipGateway.take_actor_list_ownership(movie.id)

    @classmethod
    def reset_ownership(cls, movie_number: str) -> None:
        """恢复自动管理；下一次元数据刷新会重新按远端列表替换演员。"""
        with get_database().atomic():
            movie = cls._locked_movie(movie_number)
            MovieOwnershipGateway.release_actor_list_ownership(movie.id)
