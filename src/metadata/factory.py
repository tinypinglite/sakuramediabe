"""元数据 provider 工厂函数，统一对接 provider 与本地 GFriends 能力。"""

from __future__ import annotations

import threading
from typing import Any, Iterable

from loguru import logger

from src.config.config import settings
from src.metadata._providers.dmm import DmmProvider
from src.metadata._providers.javdb import JavdbProvider
from src.metadata.gfriends import GfriendsActorImageResolver

# Resolver 单例缓存：预热任务与业务代码共享同一实例的内存 index，
# 避免"预热把 disk cache 拉下来但内存 index 只属于预热实例"的浪费。
# key 覆盖会影响构建/行为的所有字段；配置热更新（改 URL/proxy/TTL）时自动 miss 重建。
# 只保留"最近一次配置组合"对应的单个实例：当前架构下所有调用点共享同一份
# settings，key 变化即意味着配置换代，旧实例已无引用价值，miss 时直接清空避免
# 长期热更新累积内存。
_resolver_cache: dict[tuple, GfriendsActorImageResolver] = {}
_resolver_cache_lock = threading.Lock()


class GfriendsAvatarJavdbProvider:
    """只负责为 JavDB provider 返回结果补 GFriends 头像，不实现站点抓取。"""

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


def _build_gfriends_resolver(*, proxy: str | None) -> GfriendsActorImageResolver:
    key = (
        settings.metadata.gfriends_filetree_url,
        settings.metadata.gfriends_cdn_base_url,
        settings.metadata.gfriends_filetree_cache_path,
        settings.metadata.gfriends_filetree_cache_ttl_hours,
        proxy,
    )
    cached = _resolver_cache.get(key)
    if cached is not None:
        return cached
    with _resolver_cache_lock:
        cached = _resolver_cache.get(key)
        if cached is not None:
            return cached
        # 配置组合变了，旧实例不会再被业务命中，清掉避免热更新场景下累积。
        _resolver_cache.clear()
        resolver = GfriendsActorImageResolver(
            filetree_url=settings.metadata.gfriends_filetree_url,
            cdn_base_url=settings.metadata.gfriends_cdn_base_url,
            cache_path=settings.metadata.gfriends_filetree_cache_path,
            cache_ttl_hours=settings.metadata.gfriends_filetree_cache_ttl_hours,
            proxy=proxy,
        )
        _resolver_cache[key] = resolver
        return resolver


def refresh_gfriends_filetree(*, force: bool = False) -> dict[str, Any]:
    """APS 任务入口：拉取 GFriends Filetree 并写入本地缓存，返回统计信息。

    force=False 时若 disk cache 新鲜（未超过 TTL）则跳过网络请求；
    force=True 无条件重新拉取。使用当前 settings 中配置的代理与地址。
    """
    resolver = _build_gfriends_resolver(proxy=settings.metadata.gfriends_proxy)
    return resolver.refresh(force=force)


def build_javdb_provider() -> GfriendsAvatarJavdbProvider:
    """构建 JavDB provider，演员头像继续优先 GFriends。

    JavDB 请求永远不走 metadata proxy：站点访问依托 ``settings.metadata.javdb_host``
    自身的直连/反代能力，叠加代理反而绕远路甚至失败。GFriends CDN 单独沿用
    ``settings.metadata.gfriends_proxy``，与 JavDB 无关。
    """
    provider = JavdbProvider(
        host=settings.metadata.javdb_host,
        proxy=None,
        username=settings.metadata.javdb_username,
        password=settings.metadata.javdb_password,
    )
    return GfriendsAvatarJavdbProvider(
        provider=provider,
        actor_image_resolver=_build_gfriends_resolver(
            proxy=settings.metadata.gfriends_proxy,
        ),
    )


def build_dmm_provider() -> DmmProvider:
    return DmmProvider(
        proxy=settings.metadata.normalized_proxy,
    )
