"""遍历订阅演员，调用所有声明演员影片列表能力的元数据插件并导入缺失番号。"""

from __future__ import annotations

from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict

from src.common import normalize_movie_number
from src.common.service_helpers import find_movie_by_number
from src.i18n import translate
from src.model import Actor, MovieActor
from src.model.catalog.actors import split_actor_alias_name
from src.model.catalog.movies import MOVIE_ACTOR_LIST_FIELD
from src.plugins.extensions.metadata import (
    PluginActorMoviePage,
    PluginActorMovieQuery,
    PluginActorRef,
)
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.catalog.metadata_source_service import MetadataSourceService
from src.service.catalog.movie_ownership_gateway import MANUAL_MOVIE_FIELD_OWNER

MAX_PAGES_PER_SOURCE = 500


class ActorSubscriptionSyncParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["incremental", "full"] = "incremental"


class SubscribedActorMovieSyncService:
    TASK_KEY = "actor_subscription_sync"

    def __init__(
        self,
        *,
        import_service: CatalogImportService | None = None,
    ):
        self._import_service = import_service

    @property
    def import_service(self) -> CatalogImportService:
        if self._import_service is None:
            self._import_service = CatalogImportService()
        return self._import_service

    @staticmethod
    def _actor_ref(actor: Actor) -> PluginActorRef:
        primary_key = (actor.name or "").strip().casefold()
        alias_names = [
            name
            for name in split_actor_alias_name(actor.alias_name)
            if name.casefold() != primary_key
        ]
        return PluginActorRef(
            actor_id=actor.id,
            name=actor.name,
            alias_names=alias_names,
            gender=actor.gender,
        )

    @staticmethod
    def _subscribed_actors() -> list[Actor]:
        return list(
            Actor.select(Actor.id, Actor.name, Actor.alias_name, Actor.gender)
            .where(
                Actor.is_subscribed == True,
                Actor.merged_into.is_null(),
            )
            .order_by(Actor.id)
        )

    @staticmethod
    def _fetch_source_numbers(
        fetch_page,
        actor: PluginActorRef,
        mode: Literal["incremental", "full"],
    ) -> tuple[list[str] | None, Exception | None]:
        numbers: list[str] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        try:
            for _ in range(MAX_PAGES_PER_SOURCE):
                result = fetch_page(
                    PluginActorMovieQuery(actor=actor, cursor=cursor, mode=mode)
                )
                if result is None:
                    return (numbers or None), None
                page = PluginActorMoviePage.model_validate(result)
                numbers.extend(page.movie_numbers)
                if page.next_cursor is None:
                    return numbers, None
                if page.next_cursor in seen_cursors:
                    raise ValueError("插件返回了重复的演员影片游标")
                seen_cursors.add(page.next_cursor)
                cursor = page.next_cursor
            raise ValueError(
                f"插件演员影片翻页超过上限 {MAX_PAGES_PER_SOURCE} 页"
            )
        except Exception as exc:
            return (numbers or None), exc

    @staticmethod
    def _ensure_actor_link(movie, actor: Actor) -> bool:
        owners = movie.field_owners or {}
        if owners.get(MOVIE_ACTOR_LIST_FIELD) == MANUAL_MOVIE_FIELD_OWNER:
            return False
        try:
            _, created = MovieActor.get_or_create(movie=movie, actor=actor)
        except Exception as exc:
            logger.warning(
                "Actor movie sync link failed actor_id={} movie_id={} detail={}",
                actor.id,
                movie.id,
                exc,
            )
            return False
        return created

    @staticmethod
    def _emit(reporter, **payload) -> None:
        if reporter is not None:
            reporter.emit(**payload)

    def run(self, reporter=None, params=None) -> dict[str, Any]:
        config = (
            params
            if isinstance(params, ActorSubscriptionSyncParams)
            else ActorSubscriptionSyncParams.model_validate(params or {})
        )
        sources = MetadataSourceService.enabled_actor_movie_sources()
        actors = self._subscribed_actors()
        stats: dict[str, Any] = {
            "status": "ok" if sources else "skipped",
            "reason": None if sources else "no_actor_movie_source",
            "mode": config.mode,
            "sources": [entry[0] for entry in sources],
            "total_actors": len(actors),
            "processed_actors": 0,
            "failed_actors": 0,
            "unresolved_actors": 0,
            "source_calls": 0,
            "source_failures": 0,
            "discovered_movies": 0,
            "existing_movies": 0,
            "imported_movies": 0,
            "failed_movies": 0,
            "linked_movies": 0,
        }
        logger.info(
            "Subscribed actor movie sync start actors={} sources={} mode={}",
            len(actors),
            stats["sources"],
            config.mode,
        )
        if not sources:
            logger.info("Subscribed actor movie sync skipped: no capable source")
            return stats

        self.import_service.reset_actor_lookup()
        discovered_keys: set[str] = set()
        self._emit(
            reporter,
            current=0,
            total=len(actors),
            text=translate(
                "tasks.actor_subscription_sync.progress.start",
                total=len(actors),
            ),
            summary_patch=stats,
        )

        for index, actor in enumerate(actors, start=1):
            actor_ref = self._actor_ref(actor)
            movie_numbers: dict[str, str] = {}
            resolved = False
            actor_source_failures = 0

            for plugin_id, _name, _name_i18n, source in sources:
                fetch_page = source.fetch_actor_movies
                if fetch_page is None:
                    continue
                stats["source_calls"] += 1
                numbers, error = self._fetch_source_numbers(
                    fetch_page, actor_ref, config.mode
                )
                if error is not None:
                    actor_source_failures += 1
                    stats["source_failures"] += 1
                    logger.warning(
                        "Subscribed actor movie source failed plugin={} actor_id={} "
                        "actor_name={} detail={}",
                        plugin_id,
                        actor.id,
                        actor.name,
                        error,
                    )
                if numbers is None:
                    continue
                resolved = True
                for number in numbers:
                    key = normalize_movie_number(number)
                    if key and key not in movie_numbers:
                        movie_numbers[key] = number

            discovered = len(movie_numbers)
            new_keys = set(movie_numbers) - discovered_keys
            stats["discovered_movies"] += len(new_keys)
            discovered_keys.update(movie_numbers)
            if not resolved:
                if actor_source_failures:
                    stats["failed_actors"] += 1
                else:
                    stats["unresolved_actors"] += 1
            else:
                for number in movie_numbers.values():
                    existing = find_movie_by_number(number)
                    if existing is not None:
                        stats["existing_movies"] += 1
                        if self._ensure_actor_link(existing, actor):
                            stats["linked_movies"] += 1
                        continue
                    try:
                        movie, created = MetadataSourceService.import_by_number(
                            number,
                            self.import_service,
                            force_subscribed=False,
                        )
                    except Exception as exc:
                        stats["failed_movies"] += 1
                        logger.warning(
                            "Subscribed actor movie import failed actor_id={} "
                            "actor_name={} movie_number={} detail={}",
                            actor.id,
                            actor.name,
                            number,
                            exc,
                        )
                        continue
                    if created:
                        stats["imported_movies"] += 1
                    else:
                        stats["existing_movies"] += 1
                    if self._ensure_actor_link(movie, actor):
                        stats["linked_movies"] += 1

            stats["processed_actors"] = index
            logger.info(
                "Subscribed actor movie sync actor finished actor_id={} "
                "actor_name={} resolved={} discovered={} imported={} failed={}",
                actor.id,
                actor.name,
                resolved,
                discovered,
                stats["imported_movies"],
                stats["failed_movies"],
            )
            self._emit(
                reporter,
                current=index,
                total=len(actors),
                text=translate(
                    "tasks.actor_subscription_sync.progress.actor",
                    actor_name=actor.name,
                    discovered=discovered,
                    imported=stats["imported_movies"],
                    failed=stats["failed_movies"],
                ),
                summary_patch=stats,
            )

        self._emit(
            reporter,
            current=len(actors),
            total=len(actors),
            text=translate(
                "tasks.actor_subscription_sync.progress.finished",
                actors=stats["processed_actors"],
                imported=stats["imported_movies"],
                failed=stats["failed_movies"],
            ),
            summary_patch=stats,
        )
        logger.info("Subscribed actor movie sync finished stats={}", stats)
        return stats
