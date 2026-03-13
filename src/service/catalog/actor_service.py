"""演员目录 service。

负责演员列表、详情、关联影片/标签/年份查询，以及按演员名从 JavDB 搜索并导入。
阅读入口建议从 ``list_actors``、``get_actor_movies``、``stream_search_and_upsert_actor_from_javdb`` 开始。
"""

from typing import Iterator, List, Set

from loguru import logger
from peewee import JOIN, MySQLDatabase, PostgresqlDatabase, SqliteDatabase, fn

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.metadata.gfriends import GfriendsActorImageResolver
from src.metadata.javdb import JavdbProvider
from src.metadata.provider import MetadataNotFoundError
from src.model import Actor, Image, Media, Movie, MovieActor, MovieTag, Tag, get_database
from src.schema.catalog.actors import (
    ActorDetailResource,
    ActorListGender,
    ActorListSubscriptionStatus,
    ActorResource,
    YearResource,
)
from src.schema.catalog.movies import ActorMovieResource, TagResource
from src.schema.common.pagination import PageResponse
from src.schema.metadata.javdb import JavdbMovieActorResource
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError


class ActorService:
    """聚合 Actor 查询和 JavDB 演员导入流程。"""

    FEMALE_GENDER = 1
    MALE_GENDER = 2

    @staticmethod
    def _actor_query():
        """演员基础查询统一补齐头像，避免调用方重复 join。"""
        return (
            Actor.select(Actor, Image)
            .join(Image, JOIN.LEFT_OUTER, on=(Actor.profile_image == Image.id))
            .order_by(Actor.id)
        )

    @classmethod
    def _filtered_actors(
        cls,
        gender: ActorListGender = ActorListGender.ALL,
        subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
    ):
        """演员列表筛选统一收口到这里，保证 count 和 items 逻辑一致。"""
        query = cls._actor_query()

        if gender == ActorListGender.FEMALE:
            query = query.where(Actor.gender == cls.FEMALE_GENDER)
        elif gender == ActorListGender.MALE:
            query = query.where(Actor.gender == cls.MALE_GENDER)

        if subscription_status == ActorListSubscriptionStatus.SUBSCRIBED:
            query = query.where(Actor.is_subscribed == True)
        elif subscription_status == ActorListSubscriptionStatus.UNSUBSCRIBED:
            query = query.where(Actor.is_subscribed == False)

        return query

    @classmethod
    def _require_actor(cls, actor_id: int) -> Actor:
        actor = cls._actor_query().where(Actor.id == actor_id).get_or_none()
        if actor is None:
            raise ApiError(404, "actor_not_found", "演员不存在", {"actor_id": actor_id})
        return actor

    @staticmethod
    def _movie_query(actor_id: int):
        """返回某个演员关联影片的基础查询，并补齐封面。"""
        return (
            Movie.select(Movie, Image)
            .join(Image, JOIN.LEFT_OUTER, on=(Movie.cover_image == Image.id))
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == actor_id)
        )

    @staticmethod
    def _playable_movie_numbers(movie_numbers: List[str]) -> Set[str]:
        """批量查出哪些影片至少有一条有效媒体。"""
        if not movie_numbers:
            return set()
        query = (
            Media.select(Media.movie)
            .where(
                Media.valid == True,
                Media.movie.in_(movie_numbers),
            )
            .distinct()
            .tuples()
        )
        return {movie_number for movie_number, in query}

    @classmethod
    def _attach_can_play(cls, movies: List[Movie]) -> None:
        playable_movie_numbers = cls._playable_movie_numbers([movie.movie_number for movie in movies])
        for movie in movies:
            movie.can_play = movie.movie_number in playable_movie_numbers

    @staticmethod
    def _year_expression():
        """按当前数据库方言构建“上映年份”表达式。"""
        database = get_database()
        if isinstance(database, SqliteDatabase):
            return fn.strftime("%Y", Movie.release_date)
        if isinstance(database, MySQLDatabase):
            return fn.YEAR(Movie.release_date)
        if isinstance(database, PostgresqlDatabase):
            return fn.DATE_PART("year", Movie.release_date)
        raise RuntimeError(f"Unsupported database type: {type(database)!r}")

    @classmethod
    def list_actors(
        cls,
        gender: ActorListGender = ActorListGender.ALL,
        subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[ActorResource]:
        start = max(page - 1, 0) * page_size
        total = cls._filtered_actors(gender=gender, subscription_status=subscription_status).count()
        actors = list(
            cls._filtered_actors(gender=gender, subscription_status=subscription_status)
            .offset(start)
            .limit(page_size)
        )
        return PageResponse[ActorResource](
            items=ActorResource.from_items(actors),
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def search_local_actors(cls, query: str) -> list[ActorResource]:
        normalized = query.strip().lower()
        if not normalized:
            return []
        actors = list(
            cls._actor_query().where(
                (fn.LOWER(Actor.name).contains(normalized))
                | (fn.LOWER(Actor.alias_name).contains(normalized))
            )
        )
        return ActorResource.from_items(actors)

    @staticmethod
    def _build_javdb_provider() -> JavdbProvider:
        metadata_proxy = (settings.metadata.proxy or "").strip() or None
        actor_image_resolver = GfriendsActorImageResolver(
            filetree_url=settings.metadata.gfriends_filetree_url,
            cdn_base_url=settings.metadata.gfriends_cdn_base_url,
            cache_path=settings.metadata.gfriends_filetree_cache_path,
            cache_ttl_hours=settings.metadata.gfriends_filetree_cache_ttl_hours,
            proxy=metadata_proxy,
        )
        return JavdbProvider(
            host=settings.metadata.javdb_host,
            proxy=metadata_proxy,
            actor_image_resolver=actor_image_resolver,
        )

    @classmethod
    def _build_catalog_import_service(cls) -> CatalogImportService:
        return CatalogImportService()

    @classmethod
    def stream_search_and_upsert_actor_from_javdb(
        cls,
        actor_name: str,
    ) -> Iterator[tuple[str, dict]]:
        """按 SSE 事件顺序输出演员搜索和导入进度。"""
        normalized_name = actor_name.strip()
        yield "search_started", {"actor_name": normalized_name}

        try:
            actor_resources = cls._build_javdb_provider().search_actors(normalized_name)
        except MetadataNotFoundError:
            yield "completed", {"success": False, "reason": "actor_not_found", "actors": []}
            return
        except Exception as exc:
            logger.exception("Javdb actor search failed actor_name={} detail={}", normalized_name, exc)
            yield "completed", {"success": False, "reason": "internal_error", "actors": []}
            return

        # JavDB 搜索结果可能包含重复演员卡片，这里先按 javdb_id 去重，再进入导入阶段。
        deduplicated_resources: list[JavdbMovieActorResource] = []
        seen_javdb_ids: set[str] = set()
        for actor_resource in actor_resources:
            if actor_resource.javdb_id in seen_javdb_ids:
                continue
            seen_javdb_ids.add(actor_resource.javdb_id)
            deduplicated_resources.append(actor_resource)

        total = len(deduplicated_resources)
        yield "actor_found", {
            "actors": [
                {
                    "javdb_id": actor_resource.javdb_id,
                    "name": actor_resource.name,
                    "avatar_url": actor_resource.avatar_url,
                }
                for actor_resource in deduplicated_resources
            ],
            "total": total,
        }

        yield "upsert_started", {"total": total}

        created_count = 0
        already_exists_count = 0
        failed_count = 0
        failed_items: list[dict] = []
        imported_actors: list[ActorResource] = []
        import_service = cls._build_catalog_import_service()

        for index, actor_resource in enumerate(deduplicated_resources, start=1):
            # 图片下载是前端最关心的慢步骤，单独发事件便于展示进度。
            yield "image_download_started", {
                "javdb_id": actor_resource.javdb_id,
                "index": index,
                "total": total,
            }
            existed_before_upsert = Actor.get_or_none(Actor.javdb_id == actor_resource.javdb_id) is not None
            try:
                actor = import_service.upsert_actor_from_javdb_resource(actor_resource)
                actor_with_profile = cls._actor_query().where(Actor.id == actor.id).get_or_none() or actor
                imported_actors.append(ActorResource.from_attributes_model(actor_with_profile))
                if existed_before_upsert:
                    already_exists_count += 1
                else:
                    created_count += 1
                yield "image_download_finished", {
                    "javdb_id": actor_resource.javdb_id,
                    "index": index,
                    "total": total,
                    "has_avatar": bool(actor_resource.avatar_url),
                }
            except ImageDownloadError as exc:
                failed_count += 1
                logger.warning(
                    "Javdb actor image download failed actor_name={} javdb_id={} detail={}",
                    normalized_name,
                    actor_resource.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": actor_resource.javdb_id,
                        "reason": "image_download_failed",
                        "detail": str(exc),
                    }
                )
            except Exception as exc:
                failed_count += 1
                logger.exception(
                    "Javdb actor upsert failed actor_name={} javdb_id={} detail={}",
                    normalized_name,
                    actor_resource.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": actor_resource.javdb_id,
                        "reason": "upsert_failed",
                        "detail": str(exc),
                    }
                )

        stats = {
            "total": total,
            "created_count": created_count,
            "already_exists_count": already_exists_count,
            "failed_count": failed_count,
        }
        yield "upsert_finished", stats

        if imported_actors:
            yield "completed", {
                "success": True,
                "actors": [actor.model_dump() for actor in imported_actors],
                "failed_items": failed_items,
                "stats": stats,
            }
            return

        yield "completed", {
            "success": False,
            "reason": "internal_error",
            "actors": [],
            "failed_items": failed_items,
            "stats": stats,
        }

    @classmethod
    def get_actor_detail(cls, actor_id: int) -> ActorDetailResource:
        actor = cls._require_actor(actor_id)
        return ActorDetailResource.from_attributes_model(actor)

    @classmethod
    def set_subscription(cls, actor_id: int, subscribed: bool) -> None:
        updated = (
            Actor.update(is_subscribed=subscribed)
            .where(Actor.id == actor_id)
            .execute()
        )
        if updated == 0:
            raise ApiError(404, "actor_not_found", "演员不存在", {"actor_id": actor_id})

    @classmethod
    def get_actor_movie_ids(cls, actor_id: int) -> list[int]:
        cls._require_actor(actor_id)
        query = (
            Movie.select(Movie.id)
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == actor_id)
            .order_by(Movie.id)
        )
        return [movie.id for movie in query]

    @classmethod
    def get_actor_movies(
        cls,
        actor_id: int,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[ActorMovieResource]:
        cls._require_actor(actor_id)
        start = max(page - 1, 0) * page_size
        total = MovieActor.select().where(MovieActor.actor == actor_id).count()
        movies = list(
            cls._movie_query(actor_id)
            .order_by(Movie.movie_number)
            .offset(start)
            .limit(page_size)
        )
        # can_play 依赖 Media 表，统一在列表结果上批量补，避免在主查询里引入额外重复行。
        cls._attach_can_play(movies)
        return PageResponse[ActorMovieResource](
            items=ActorMovieResource.from_items(movies),
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_actor_tags(cls, actor_id: int) -> list[TagResource]:
        cls._require_actor(actor_id)
        query = (
            Tag.select(Tag)
            .join(MovieTag)
            .join(Movie, on=(MovieTag.movie == Movie.id))
            .join(MovieActor, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == actor_id)
            .distinct()
            .order_by(Tag.name)
        )
        return [TagResource(tag_id=tag.id, name=tag.name) for tag in query]

    @classmethod
    def get_actor_years(cls, actor_id: int) -> list[YearResource]:
        cls._require_actor(actor_id)
        year_expression = cls._year_expression()
        query = (
            Movie.select(year_expression.alias("year"))
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(
                MovieActor.actor == actor_id,
                Movie.release_date.is_null(False),
            )
            .distinct()
            .order_by(year_expression.desc())
        )
        years = []
        for row in query:
            # 不同数据库对年份表达式的返回类型不完全一致，统一转成 int 再交给 schema。
            year = row.year
            if year is None:
                continue
            years.append(int(year))
        return [YearResource(year=year) for year in years]
