"""把插件演员名称解析为本地演员，并补齐 GFriends 头像。"""

from __future__ import annotations

import re
import unicodedata
from threading import RLock

from loguru import logger

from src.metadata.gfriends import GfriendsActorImageResolver, get_gfriends_directory
from src.model import Actor
from src.model.catalog.actors import (
    merge_actor_alias_name,
    split_actor_alias_name,
)
from src.plugins.extensions.metadata import PluginMetadataActor
from src.service.catalog.actor_ownership_gateway import ActorOwnershipGateway
from src.service.catalog.movie_image_service import MovieImageService


def _normalize_actor_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip().casefold()
    return re.sub(r"\s+", "", normalized)


class ActorLookupIndex:
    """批次内复用的演员归一化索引；更新操作与索引查询共用一把锁。"""

    def __init__(self) -> None:
        self.lock = RLock()
        self._loaded = False
        self._primary: dict[str, list[Actor]] = {}
        self._aliases: dict[str, list[Actor]] = {}
        self._keys_by_actor: dict[int, tuple[set[str], set[str]]] = {}

    @classmethod
    def load(cls) -> ActorLookupIndex:
        index = cls()
        with index.lock:
            index._ensure_loaded_locked()
        return index

    def invalidate(self) -> None:
        with self.lock:
            self._loaded = False
            self._primary.clear()
            self._aliases.clear()
            self._keys_by_actor.clear()

    def find_existing(self, names: list[str]) -> Actor | None:
        wanted = {
            _normalize_actor_name(name)
            for name in names
            if name and name.strip()
        }
        if not wanted:
            return None
        with self.lock:
            self._ensure_loaded_locked()
            primary_matches = [
                actor
                for key in wanted
                for actor in self._primary.get(key, ())
            ]
            if primary_matches:
                return min(primary_matches, key=lambda actor: actor.id)
            alias_matches = [
                actor
                for key in wanted
                for actor in self._aliases.get(key, ())
            ]
            if alias_matches:
                return min(alias_matches, key=lambda actor: actor.id)
            return None

    def add_or_refresh(self, actor: Actor) -> None:
        with self.lock:
            self._ensure_loaded_locked()
            self._remove_actor_locked(actor.id)
            primary_keys, alias_keys = self._actor_keys(actor)
            self._keys_by_actor[actor.id] = (primary_keys, alias_keys)
            for key in primary_keys:
                self._primary.setdefault(key, []).append(actor)
            for key in alias_keys:
                self._aliases.setdefault(key, []).append(actor)

    def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        actors = list(
            Actor.select().where(Actor.merged_into.is_null()).order_by(Actor.id)
        )
        self._primary.clear()
        self._aliases.clear()
        self._keys_by_actor.clear()
        for actor in actors:
            primary_keys, alias_keys = self._actor_keys(actor)
            self._keys_by_actor[actor.id] = (primary_keys, alias_keys)
            for key in primary_keys:
                self._primary.setdefault(key, []).append(actor)
            for key in alias_keys:
                self._aliases.setdefault(key, []).append(actor)
        self._loaded = True

    @staticmethod
    def _actor_keys(actor: Actor) -> tuple[set[str], set[str]]:
        primary_key = _normalize_actor_name(actor.name)
        primary_keys = {primary_key} if primary_key else set()
        alias_keys = {
            key
            for alias in split_actor_alias_name(actor.alias_name)
            if (key := _normalize_actor_name(alias))
        }
        return primary_keys, alias_keys

    def _remove_actor_locked(self, actor_id: int) -> None:
        keys = self._keys_by_actor.pop(actor_id, None)
        if keys is None:
            return
        primary_keys, alias_keys = keys
        for key in primary_keys:
            self._drop_actor(self._primary.get(key), actor_id)
        for key in alias_keys:
            self._drop_actor(self._aliases.get(key), actor_id)

    @staticmethod
    def _drop_actor(bucket: list[Actor] | None, actor_id: int) -> None:
        if bucket is None:
            return
        bucket[:] = [actor for actor in bucket if actor.id != actor_id]


class ActorResolutionService:
    @classmethod
    def resolve_movie_actors(
        cls,
        actors: list[PluginMetadataActor],
        *,
        image_service: MovieImageService | None = None,
        lookup: ActorLookupIndex | None = None,
    ) -> list[Actor]:
        resolved: list[Actor] = []
        seen_ids: set[int] = set()
        owns_service = image_service is None
        service = image_service or MovieImageService()
        lookup = lookup or ActorLookupIndex.load()
        try:
            for plugin_actor in actors:
                names = [plugin_actor.name, *plugin_actor.alias_names]
                with lookup.lock:
                    actor = lookup.find_existing(names)
                    if actor is None:
                        actor = Actor.create(
                            name=plugin_actor.name,
                            alias_name=merge_actor_alias_name(
                                plugin_actor.name,
                                plugin_actor.alias_names,
                                "",
                            ),
                            gender=plugin_actor.gender,
                        )
                    else:
                        cls._merge_aliases(
                            actor, plugin_actor.name, plugin_actor.alias_names
                        )
                        cls._fill_unknown_gender(actor, plugin_actor.gender)
                    lookup.add_or_refresh(actor)
                cls.ensure_avatar(actor, names, image_service=service)
                if actor.id not in seen_ids:
                    seen_ids.add(actor.id)
                    resolved.append(actor)
        finally:
            if owns_service:
                service.http_client.close()
        return resolved

    @staticmethod
    def _fill_unknown_gender(actor: Actor, gender: int) -> None:
        """只补齐未知性别；人工归属由受控写入层保护，已有性别不覆盖。"""
        if actor.gender != 0 or gender not in {1, 2}:
            return
        if ActorOwnershipGateway.update_host_source(actor.id, {"gender": gender}):
            actor.gender = gender

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
    def find_existing(
        cls,
        names: list[str],
        *,
        lookup: ActorLookupIndex | None = None,
    ) -> Actor | None:
        index = lookup or ActorLookupIndex.load()
        return index.find_existing(names)

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
        return _normalize_actor_name(value)
