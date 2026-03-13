from datetime import datetime
from typing import Dict, List

from loguru import logger

from src.config.config import settings
from src.metadata.gfriends import GfriendsActorImageResolver
from src.metadata.javdb import JavdbProvider
from src.model import Actor, Movie, MovieActor
from src.service.catalog.catalog_import_service import CatalogImportService


class SubscribedActorMovieSyncService:
    def __init__(
        self,
        provider: JavdbProvider | None = None,
        import_service: CatalogImportService | None = None,
    ):
        metadata_proxy = (settings.metadata.proxy or "").strip() or None
        actor_image_resolver = GfriendsActorImageResolver(
            filetree_url=settings.metadata.gfriends_filetree_url,
            cdn_base_url=settings.metadata.gfriends_cdn_base_url,
            cache_path=settings.metadata.gfriends_filetree_cache_path,
            cache_ttl_hours=settings.metadata.gfriends_filetree_cache_ttl_hours,
            proxy=metadata_proxy,
        )
        self.provider = provider or JavdbProvider(
            host=settings.metadata.javdb_host,
            proxy=metadata_proxy,
            actor_image_resolver=actor_image_resolver,
        )
        self.import_service = import_service or CatalogImportService()

    def sync_subscribed_actor_movies(self) -> Dict[str, int]:
        actors = list(
            Actor.select()
            .where(Actor.is_subscribed == True)
            .order_by(Actor.id)
        )
        logger.info("Subscribed actor sync start actors={}", len(actors))
        stats = {
            "total_actors": len(actors),
            "success_actors": 0,
            "failed_actors": 0,
            "imported_movies": 0,
        }
        for actor in actors:
            try:
                actor_stats = self._sync_actor(actor)
                stats["success_actors"] += 1
                stats["imported_movies"] += actor_stats["imported_movies"]
            except Exception:
                stats["failed_actors"] += 1
                logger.exception(
                    "Subscribed actor sync failed actor_id={} actor_javdb_id={} actor_name={}",
                    actor.id,
                    actor.javdb_id,
                    actor.name,
                )
        logger.info("Subscribed actor sync finished stats={}", stats)
        return stats

    def _sync_actor(self, actor: Actor) -> Dict[str, int]:
        mode = "full" if actor.subscribed_movies_full_synced_at is None else "incremental"
        page = 1
        imported_movies = 0
        stop_reason = "empty_page"

        logger.info(
            "Subscribed actor sync actor start actor_id={} actor_javdb_id={} actor_name={} mode={}",
            actor.id,
            actor.javdb_id,
            actor.name,
            mode,
        )

        while True:
            movies = self.provider.get_actor_movies_by_javdb(
                actor_javdb_id=actor.javdb_id,
                actor_type=actor.javdb_type,
                page=page,
            )
            if not movies:
                break

            should_stop = False
            for movie_item in movies:
                if mode == "incremental" and self._actor_movie_exists(actor.id, movie_item.javdb_id):
                    stop_reason = "existing_actor_movie"
                    should_stop = True
                    logger.info(
                        "Subscribed actor sync hit existing movie actor_id={} actor_javdb_id={} movie_javdb_id={}",
                        actor.id,
                        actor.javdb_id,
                        movie_item.javdb_id,
                    )
                    break

                detail = self.provider.get_movie_by_javdb_id(movie_item.javdb_id)
                self.import_service.upsert_movie_from_javdb_detail(detail)
                imported_movies += 1
                logger.info(
                    "Subscribed actor sync imported actor_id={} actor_javdb_id={} movie_javdb_id={} movie_number={}",
                    actor.id,
                    actor.javdb_id,
                    detail.javdb_id,
                    detail.movie_number,
                )

            if should_stop:
                break

            page += 1

        synced_at = datetime.utcnow()
        actor.subscribed_movies_synced_at = synced_at
        if mode == "full" and actor.subscribed_movies_full_synced_at is None:
            actor.subscribed_movies_full_synced_at = synced_at
        actor.save()
        logger.info(
            "Subscribed actor sync actor finished actor_id={} actor_javdb_id={} mode={} imported_movies={} stop_reason={} synced_at={}",
            actor.id,
            actor.javdb_id,
            mode,
            imported_movies,
            stop_reason,
            synced_at.isoformat(),
        )
        return {
            "imported_movies": imported_movies,
        }

    def _actor_movie_exists(self, actor_id: int, movie_javdb_id: str) -> bool:
        query = (
            MovieActor.select(MovieActor.id)
            .join(Movie, on=(MovieActor.movie == Movie.id))
            .where(
                MovieActor.actor == actor_id,
                Movie.javdb_id == movie_javdb_id,
            )
        )
        return query.exists()
