from typing import Any, Dict, List

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.model import Actor, Movie, MovieActor
from src.service.catalog.catalog_import_service import CatalogImportService


class SubscribedActorMovieSyncService:
    def __init__(
        self,
        provider: Any | None = None,
        import_service: CatalogImportService | None = None,
    ):
        self.provider = provider or self._build_javdb_provider()
        self.import_service = import_service or CatalogImportService()

    @staticmethod
    def _build_javdb_provider():
        from src.metadata.factory import build_javdb_provider

        return build_javdb_provider()

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    def sync_subscribed_actor_movies(self, progress_callback=None) -> Dict[str, int]:
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
        self._emit_progress(
            progress_callback,
            current=0,
            total=len(actors),
            text="开始同步已订阅演员影片",
            summary_patch=stats,
        )
        for index, actor in enumerate(actors, start=1):
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
            self._emit_progress(
                progress_callback,
                current=index,
                total=len(actors),
                text=f"已处理演员 {actor.name}",
                summary_patch=stats,
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

                try:
                    # 单片导入失败按影片跳过，避免阻断该演员剩余影片和后续演员的同步。
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
                except Exception as exc:
                    logger.warning(
                        "Subscribed actor sync skipped failed movie actor_id={} actor_javdb_id={} movie_javdb_id={} detail={}",
                        actor.id,
                        actor.javdb_id,
                        movie_item.javdb_id,
                        exc,
                    )
                    continue

            if should_stop:
                break

            page += 1

        synced_at = utc_now_for_db()
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
