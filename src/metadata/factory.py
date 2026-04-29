"""元数据 provider 工厂函数，统一对接闭源 provider 与本地 GFriends 能力。"""

from __future__ import annotations

from typing import Any, Iterable

from loguru import logger
from sakuramedia_metadata_providers.entrypoints import (
    create_dmm_provider,
    create_javdb_provider,
    create_missav_ranking_provider,
    create_missav_thumbnail_provider,
)
from sakuramedia_metadata_providers.providers.dmm import DmmProvider
from sakuramedia_metadata_providers.providers.javdb import JavdbProvider
from sakuramedia_metadata_providers.providers.missav import (
    MissavRankingProvider,
    MissavThumbnailProvider,
)

from src.config.config import settings
from src.metadata.gfriends import GfriendsActorImageResolver
from src.metadata.license_runtime import resolve_metadata_provider_license_runtime


class GfriendsAvatarJavdbProvider:
    """只负责为闭源 JavDB 返回结果补 GFriends 头像，不实现站点抓取。"""

    def __init__(self, provider: JavdbProvider, actor_image_resolver: GfriendsActorImageResolver | None):
        self.provider = provider
        self.actor_image_resolver = actor_image_resolver

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def get_movie_by_number(self, movie_number: str):
        return self._apply_detail_actor_avatars(self.provider.get_movie_by_number(movie_number))

    def get_movie_detail(self, movie_number: str):
        return self._apply_detail_actor_avatars(self.provider.get_movie_detail(movie_number))

    def get_movie_by_javdb_id(self, javdb_id: str):
        return self._apply_detail_actor_avatars(self.provider.get_movie_by_javdb_id(javdb_id))

    def search_actor(self, actor_name: str):
        return self._apply_actor_avatar(self.provider.search_actor(actor_name))

    def search_actors(self, actor_name: str):
        return [self._apply_actor_avatar(actor) for actor in self.provider.search_actors(actor_name)]

    def _apply_detail_actor_avatars(self, detail):
        for actor in getattr(detail, "actors", []) or []:
            self._apply_actor_avatar(actor)
        return detail

    def _apply_actor_avatar(self, actor):
        if self.actor_image_resolver is None:
            return actor
        candidate_names = self._actor_candidate_names(actor)
        if not candidate_names:
            return actor
        try:
            resolved_url = self.actor_image_resolver.resolve(candidate_names)
        except Exception as exc:
            logger.warning("GFriends actor avatar resolve failed actor_name={} detail={}", getattr(actor, "name", ""), exc)
            return actor
        if resolved_url:
            actor.avatar_url = resolved_url
        return actor

    @staticmethod
    def _actor_candidate_names(actor) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        raw_names: Iterable[str] = [*(getattr(actor, "alias_names", []) or []), getattr(actor, "name", "") or ""]
        for raw_name in raw_names:
            name = str(raw_name).strip()
            if not name:
                continue
            normalized_name = name.casefold()
            if normalized_name in seen:
                continue
            seen.add(normalized_name)
            names.append(name)
        return names


def _license_kwargs() -> dict[str, str | None]:
    return resolve_metadata_provider_license_runtime().as_provider_kwargs()


def _build_gfriends_resolver(*, proxy: str | None) -> GfriendsActorImageResolver:
    return GfriendsActorImageResolver(
        filetree_url=settings.metadata.gfriends_filetree_url,
        cdn_base_url=settings.metadata.gfriends_cdn_base_url,
        cache_path=settings.metadata.gfriends_filetree_cache_path,
        cache_ttl_hours=settings.metadata.gfriends_filetree_cache_ttl_hours,
        proxy=proxy,
    )


def build_javdb_provider(*, use_metadata_proxy: bool = False) -> GfriendsAvatarJavdbProvider:
    """构建 JavDB provider，站点抓取由闭源包负责，演员头像继续优先 GFriends。"""
    metadata_proxy = settings.metadata.normalized_proxy
    provider_proxy = metadata_proxy if use_metadata_proxy else None
    gfriends_proxy = metadata_proxy if use_metadata_proxy else settings.metadata.gfriends_proxy
    provider = create_javdb_provider(
        host=settings.metadata.javdb_host,
        proxy=provider_proxy,
        **_license_kwargs(),
    )
    return GfriendsAvatarJavdbProvider(
        provider=provider,
        actor_image_resolver=_build_gfriends_resolver(proxy=gfriends_proxy),
    )


def build_dmm_provider() -> DmmProvider:
    return create_dmm_provider(
        proxy=settings.metadata.normalized_proxy,
        **_license_kwargs(),
    )


def build_missav_thumbnail_provider() -> MissavThumbnailProvider:
    return create_missav_thumbnail_provider(**_license_kwargs())


def build_missav_ranking_provider() -> MissavRankingProvider:
    return create_missav_ranking_provider(**_license_kwargs())
