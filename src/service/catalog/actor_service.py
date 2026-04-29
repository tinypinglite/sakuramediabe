"""演员目录 service。

负责演员列表、详情、关联影片/标签/年份查询，以及按演员名从 JavDB 搜索并导入。
阅读入口建议从 ``list_actors``、``get_actor_movies``、``stream_search_and_upsert_actor_from_javdb`` 开始。
"""

from typing import Iterator, List, Set

from loguru import logger
from peewee import JOIN, fn

from src.api.exception.errors import ApiError
from src.common.service_helpers import (
    media_special_tag_match_expression,
    parse_special_tags_text,
    require_record,
    with_movie_card_relations,
)
from src.config.config import settings
from sakuramedia_metadata_providers.providers.javdb import JavdbProvider
from src.metadata.provider import MetadataNotFoundError
from src.model import Actor, Image, Media, Movie, MovieActor, MovieTag, Tag
from src.model.expressions import year_expression
from src.schema.catalog.actors import (
    ActorDetailResource,
    ActorListGender,
    ActorListSubscriptionStatus,
    ActorResource,
    YearResource,
)
from src.schema.catalog.movies import ActorMovieResource, MovieSpecialTagFilter, TagResource
from src.schema.common.pagination import PageResponse
from sakuramedia_metadata_providers.models import JavdbMovieActorResource
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
        return require_record(
            Actor, Actor.id == actor_id,
            error_code="actor_not_found",
            error_message="演员不存在",
            error_details={"actor_id": actor_id},
            query=cls._actor_query(),
        )

    @staticmethod
    def _movie_query(actor_id: int):
        """返回某个演员关联影片的基础查询，并补齐封面。"""
        query, _thin_cover_alias = with_movie_card_relations(
            Movie.select(Movie)
        )
        return (
            query
            .switch(Movie)
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == actor_id)
        )

    _media_special_tag_match_expression = staticmethod(media_special_tag_match_expression)
    _parse_special_tags_text = staticmethod(parse_special_tags_text)

    @classmethod
    def _filtered_actor_movies(
        cls,
        actor_id: int,
        special_tag: MovieSpecialTagFilter | None = None,
    ):
        query = cls._movie_query(actor_id)
        if special_tag is None:
            return query
        # 只让命中有效 media 特殊标签的影片进入演员作品列表。
        matched_movie_numbers = (
            Media.select(Media.movie)
            .where(
                Media.valid == True,
                cls._media_special_tag_match_expression(special_tag.to_media_tag()),
            )
            .distinct()
        )
        return query.where(Movie.movie_number.in_(matched_movie_numbers))

    @classmethod
    def _attach_movie_flags(cls, movies: List[Movie]) -> None:
        """批量补充影片是否可播放、是否 4K，避免逐条查 media。"""
        movie_numbers = [movie.movie_number for movie in movies]
        playable_movie_numbers: Set[str] = set()
        is_4k_movie_numbers: Set[str] = set()
        if not movie_numbers:
            return
        media_query = (
            Media.select(Media.movie, Media.special_tags)
            .where(
                Media.valid == True,
                Media.movie.in_(movie_numbers),
            )
            .tuples()
        )
        for movie_number, special_tags in media_query:
            playable_movie_numbers.add(movie_number)
            if "4K" in cls._parse_special_tags_text(special_tags):
                is_4k_movie_numbers.add(movie_number)

        for movie in movies:
            movie.can_play = movie.movie_number in playable_movie_numbers
            movie.is_4k = movie.movie_number in is_4k_movie_numbers

    @staticmethod
    def _year_expression():
        return year_expression(Movie.release_date)

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
        from src.metadata.factory import build_javdb_provider
        return build_javdb_provider()

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
        special_tag: MovieSpecialTagFilter | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[ActorMovieResource]:
        cls._require_actor(actor_id)
        start = max(page - 1, 0) * page_size
        total = cls._filtered_actor_movies(actor_id, special_tag=special_tag).count()
        movies = list(
            cls._filtered_actor_movies(actor_id, special_tag=special_tag)
            .order_by(Movie.movie_number)
            .offset(start)
            .limit(page_size)
        )
        cls._attach_movie_flags(movies)
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
