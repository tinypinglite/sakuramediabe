"""把插件演员名称解析为本地演员，并补齐 GFriends 头像。"""

from __future__ import annotations

import re
import unicodedata

from loguru import logger

from src.metadata.gfriends import GfriendsActorImageResolver, get_gfriends_directory
from src.model import Actor
from src.model.catalog.actors import (
    merge_actor_alias_name,
    split_actor_alias_name,
)
from src.plugins.extensions.metadata import PluginMetadataActor
from src.service.catalog.movie_image_service import MovieImageService


class ActorResolutionService:
    @classmethod
    def resolve_movie_actors(
        cls,
        actors: list[PluginMetadataActor],
        *,
        image_service: MovieImageService | None = None,
    ) -> list[Actor]:
        resolved: list[Actor] = []
        seen_ids: set[int] = set()
        owns_service = image_service is None
        service = image_service or MovieImageService()
        try:
            for plugin_actor in actors:
                names = [plugin_actor.name, *plugin_actor.alias_names]
                actor = cls.find_existing(names)
                if actor is None:
                    actor = Actor.create(
                        name=plugin_actor.name,
                        alias_name=merge_actor_alias_name(
                            plugin_actor.name,
                            plugin_actor.alias_names,
                            "",
                        ),
                        gender=0,
                    )
                else:
                    cls._merge_aliases(
                        actor, plugin_actor.name, plugin_actor.alias_names
                    )
                cls.ensure_avatar(actor, names, image_service=service)
                if actor.id not in seen_ids:
                    seen_ids.add(actor.id)
                    resolved.append(actor)
        finally:
            if owns_service:
                service.http_client.close()
        return resolved

    @classmethod
    def import_gfriends_candidate(
        cls,
        candidate_id: str,
        *,
        directory: GfriendsActorImageResolver | None = None,
        image_service: MovieImageService | None = None,
    ) -> Actor:
        directory = directory or get_gfriends_directory()
        candidate = directory.candidate(candidate_id)
        if candidate is None:
            raise ValueError("GFriends 候选无效或已过期")
        names = [candidate.name, *candidate.alias_names]
        actor = cls.find_existing(names)
        if actor is None:
            actor = Actor.create(
                name=candidate.name,
                alias_name=merge_actor_alias_name(
                    candidate.name,
                    list(candidate.alias_names),
                    "",
                ),
                gender=1,
            )
        else:
            cls._merge_aliases(actor, candidate.name, list(candidate.alias_names))
        cls.ensure_avatar(actor, names, image_service=image_service)
        return Actor.get_by_id(actor.id)

    @classmethod
    def find_existing(cls, names: list[str]) -> Actor | None:
        wanted = {cls._normalize_name(name) for name in names if name.strip()}
        if not wanted:
            return None
        actors = list(Actor.select().where(Actor.merged_into.is_null()))
        primary_matches = [
            actor for actor in actors if cls._normalize_name(actor.name) in wanted
        ]
        if primary_matches:
            return min(primary_matches, key=lambda actor: actor.id)
        alias_matches = [
            actor
            for actor in actors
            if any(
                cls._normalize_name(alias) in wanted
                for alias in split_actor_alias_name(actor.alias_name)
            )
        ]
        if alias_matches:
            return min(alias_matches, key=lambda actor: actor.id)
        return None

    @classmethod
    def ensure_avatar(
        cls,
        actor: Actor,
        names: list[str],
        *,
        image_service: MovieImageService | None = None,
    ) -> None:
        if actor.profile_image_id is not None or actor.profile_image_override_id is not None:
            return
        avatar_url = get_gfriends_directory().resolve(names)
        if not avatar_url:
            return
        service = image_service or MovieImageService()
        owns_service = image_service is None
        try:
            image = service.persist_image(
                owner_type="actor",
                owner_key=str(actor.id),
                image_url=avatar_url,
            )
            if image is None:
                return
            actor.profile_image = image
            actor.save(only=[Actor.profile_image])
        except Exception as exc:
            logger.warning(
                "GFriends actor avatar download failed actor_id={} detail={}",
                actor.id,
                exc,
            )
        finally:
            if owns_service:
                service.http_client.close()

    @classmethod
    def _merge_aliases(
        cls,
        actor: Actor,
        primary_name: str,
        alias_names: list[str],
    ) -> None:
        if (actor.field_owners or {}).get("alias_name") == "host:manual":
            return
        merged = merge_actor_alias_name(
            actor.name,
            [primary_name, *alias_names],
            actor.alias_name,
        )
        if merged != actor.alias_name:
            actor.alias_name = merged
            actor.save(only=[Actor.alias_name])

    @staticmethod
    def _normalize_name(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value or "").strip().casefold()
        return re.sub(r"\s+", "", normalized)
