from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import with_movie_card_relations
from src.model import (
    VIEW_HISTORY_ENTITY_ACTOR,
    VIEW_HISTORY_ENTITY_MOVIE,
    VIEW_HISTORY_MAX_ENTRIES,
    Actor,
    Movie,
    ViewHistory,
)
from src.schema.catalog.actors import ActorResource
from src.schema.catalog.movies import MovieListItemResource
from src.schema.catalog.view_history import (
    ViewHistoryEntryResource,
    ViewHistoryListResource,
)
from src.service.catalog.actor_service import ActorService
from src.service.catalog.movie_list_media_service import attach_movie_list_media


class ViewHistoryService:
    @classmethod
    def record(cls, entity_type: str, entity_id: int) -> None:
        """记录一次浏览；同一实体去重，只刷新最近浏览时间。"""
        now = utc_now_for_db()
        (
            ViewHistory.insert(
                entity_type=entity_type,
                entity_id=entity_id,
                viewed_at=now,
            )
            .on_conflict(
                conflict_target=[ViewHistory.entity_type, ViewHistory.entity_id],
                update={ViewHistory.viewed_at: now},
            )
            .execute()
        )
        cls._prune()

    @classmethod
    def _prune(cls) -> None:
        keep_ids = (
            ViewHistory.select(ViewHistory.id)
            .order_by(ViewHistory.viewed_at.desc(), ViewHistory.id.desc())
            .limit(VIEW_HISTORY_MAX_ENTRIES)
        )
        ViewHistory.delete().where(ViewHistory.id.not_in(keep_ids)).execute()

    @classmethod
    def list_entries(cls, limit: int) -> ViewHistoryListResource:
        rows = list(
            ViewHistory.select()
            .order_by(ViewHistory.viewed_at.desc(), ViewHistory.id.desc())
            .limit(limit)
        )
        movie_ids = [
            row.entity_id
            for row in rows
            if row.entity_type == VIEW_HISTORY_ENTITY_MOVIE
        ]
        actor_ids = [
            row.entity_id
            for row in rows
            if row.entity_type == VIEW_HISTORY_ENTITY_ACTOR
        ]
        movies = cls._load_movie_resources(movie_ids)
        actors = cls._load_actor_resources(actor_ids)

        items: list[ViewHistoryEntryResource] = []
        seen_actor_ids: set[int] = set()
        for row in rows:
            if row.entity_type == VIEW_HISTORY_ENTITY_MOVIE:
                movie = movies.get(row.entity_id)
                # 实体已删除的记录直接过滤，不占用列表位。
                if movie is None:
                    continue
                items.append(
                    ViewHistoryEntryResource(
                        entity_type=row.entity_type,
                        viewed_at=row.viewed_at,
                        movie=movie,
                    )
                )
                continue
            actor = actors.get(row.entity_id)
            # 合并产生的多条记录已解析到同一演员；rows 按时间倒序，保留最新一条。
            if actor is None or actor.id in seen_actor_ids:
                continue
            seen_actor_ids.add(actor.id)
            items.append(
                ViewHistoryEntryResource(
                    entity_type=row.entity_type,
                    viewed_at=row.viewed_at,
                    actor=actor,
                )
            )
        return ViewHistoryListResource(items=items)

    @classmethod
    def _load_movie_resources(
        cls, movie_ids: list[int]
    ) -> dict[int, MovieListItemResource]:
        if not movie_ids:
            return {}
        query, _thin_cover_alias = with_movie_card_relations(
            Movie.select().where(Movie.id.in_(movie_ids))
        )
        movies = list(query)
        # 与影片列表页同一套批量装配（含 can_play / media_items），单条 SELECT 取整页媒体。
        attach_movie_list_media(movies)
        return {
            resource.id: resource
            for resource in MovieListItemResource.from_items(movies)
        }

    @classmethod
    def _load_actor_resources(
        cls, actor_ids: list[int]
    ) -> dict[int, ActorResource]:
        if not actor_ids:
            return {}
        # 浏览记录可能指向已合并演员的墓碑；读时收敛到保留记录（合并时墓碑链已打平）。
        canonical_by_source = {
            actor.id: (actor.merged_into_id or actor.id)
            for actor in Actor.select().where(Actor.id.in_(actor_ids))
        }
        canonical_ids = set(canonical_by_source.values())
        # 演员基础查询自带头像 join 与影片数，直接复用，避免逐行统计。
        resources = {
            actor.id: ActorResource.from_actor(actor)
            for actor in ActorService.actor_query().where(Actor.id.in_(canonical_ids))
        }
        return {
            source_id: resources[canonical_id]
            for source_id, canonical_id in canonical_by_source.items()
            if canonical_id in resources
        }
