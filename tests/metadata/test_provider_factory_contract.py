from dataclasses import dataclass, field

import pytest
from src.metadata._providers.models import JavdbMovieActorResource, JavdbMovieDetailResource
from src.metadata._providers.dmm import DmmProvider

from src.config.config import settings
from src.metadata import factory as factory_module
from src.metadata.factory import (
    GfriendsAvatarJavdbProvider,
    build_dmm_provider,
    build_javdb_provider,
    refresh_gfriends_filetree,
)


@pytest.fixture(autouse=True)
def _clear_gfriends_resolver_cache():
    # 单例缓存跨测试可能污染断言；每个 case 前后都清一次。
    factory_module._resolver_cache.clear()
    yield
    factory_module._resolver_cache.clear()


@dataclass
class CapturedProvider:
    kwargs: dict
    actors: list[JavdbMovieActorResource] = field(default_factory=list)

    def get_movie_by_number(self, movie_number: str):
        return _build_detail(self.actors)

    def get_movie_detail(self, movie_number: str):
        return _build_detail(self.actors)

    def get_movie_by_javdb_id(self, javdb_id: str):
        return _build_detail(self.actors)

    def search_actor(self, actor_name: str):
        return self.actors[0]

    def search_actors(self, actor_name: str):
        return self.actors


def _build_detail(actors: list[JavdbMovieActorResource]):
    return JavdbMovieDetailResource(
        javdb_id="movie-1",
        movie_number="ABP-001",
        title="ABP-001",
        duration_minutes=120,
        summary="summary",
        actors=actors,
        tags=[],
    )


def test_build_dmm_provider_passes_site_proxy(monkeypatch):
    monkeypatch.setattr(settings.metadata, "proxy", "  http://site-proxy:7890  ")

    provider = build_dmm_provider()

    assert provider is not None
    assert isinstance(provider, DmmProvider)
    assert provider.proxy == "http://site-proxy:7890"


def test_build_javdb_provider_never_uses_metadata_proxy(monkeypatch):
    # JavDB 请求永远直连：配了 metadata proxy 也不能叠加到 JavDB 上，
    # 站点访问依托 javdb_host 自身的直连/反代能力。
    monkeypatch.setattr(settings.metadata, "proxy", "  http://site-proxy:7890  ")

    provider = build_javdb_provider()

    assert isinstance(provider, GfriendsAvatarJavdbProvider)
    assert provider.provider.host == settings.metadata.javdb_host
    assert provider.provider.proxy is None


def test_build_javdb_provider_passes_account_credentials(monkeypatch):
    monkeypatch.setattr(settings.metadata, "javdb_username", "user@example.com")
    monkeypatch.setattr(settings.metadata, "javdb_password", "secret")

    provider = build_javdb_provider()

    assert provider.provider.username == "user@example.com"
    assert provider.provider.password == "secret"


def test_javdb_adapter_prefers_gfriends_avatar():
    actor = JavdbMovieActorResource(
        javdb_id="actor-1",
        name="桥本有菜",
        alias_names=["Arina Hashimoto"],
        avatar_url="https://javdb.example/avatar.jpg",
    )

    class FakeResolver:
        def __init__(self):
            self.candidate_names = None

        def resolve(self, candidate_names):
            self.candidate_names = candidate_names
            return "https://gfriends.example/avatar.jpg"

    resolver = FakeResolver()
    provider = GfriendsAvatarJavdbProvider(CapturedProvider(kwargs={}, actors=[actor]), resolver)

    detail = provider.get_movie_by_number("ABP-001")

    assert detail.actors[0].avatar_url == "https://gfriends.example/avatar.jpg"
    assert resolver.candidate_names == ["Arina Hashimoto", "桥本有菜"]


def test_gfriends_resolver_is_singleton_per_config_key(monkeypatch):
    monkeypatch.setattr(settings.metadata, "proxy", None)

    provider_a = build_javdb_provider()
    provider_b = build_javdb_provider()

    # 同一 (url, cdn, cache_path, ttl, proxy) 组合下应命中缓存返回同实例，
    # 让预热任务写入的内存 index 能被业务侧直接看到。
    assert provider_a.actor_image_resolver is provider_b.actor_image_resolver


def test_gfriends_resolver_rebuilt_when_proxy_changes(monkeypatch):
    # GFriends 沿用 metadata.proxy；配置换代时 resolver 应重建，防止 stale 代理。
    monkeypatch.setattr(settings.metadata, "proxy", None)
    resolver_no_proxy = build_javdb_provider().actor_image_resolver

    monkeypatch.setattr(settings.metadata, "proxy", "http://site-proxy:7890")
    resolver_with_proxy = build_javdb_provider().actor_image_resolver

    assert resolver_no_proxy is not resolver_with_proxy


def test_gfriends_resolver_cache_evicts_previous_config(monkeypatch):
    # 配置换代后旧实例必须被 evict，避免长期热更新累积无引用的 resolver 内存。
    monkeypatch.setattr(settings.metadata, "proxy", None)
    build_javdb_provider()

    monkeypatch.setattr(settings.metadata, "proxy", "http://site-proxy:7890")
    build_javdb_provider()

    assert len(factory_module._resolver_cache) == 1


def test_refresh_gfriends_filetree_delegates_to_resolver(monkeypatch):
    monkeypatch.setattr(settings.metadata, "proxy", None)

    calls: list[bool] = []

    def _fake_refresh(*, force: bool):
        calls.append(force)
        return {"entries": 42, "source": "network", "bytes_written": 1024, "force": force}

    # 拿到 factory 会 build 的同一个 resolver 实例后打桩其 refresh。
    resolver = build_javdb_provider().actor_image_resolver
    monkeypatch.setattr(resolver, "refresh", _fake_refresh)

    stats = refresh_gfriends_filetree(force=True)

    assert calls == [True]
    assert stats["entries"] == 42
    assert stats["source"] == "network"


def test_javdb_adapter_keeps_original_avatar_when_gfriends_fails():
    actor = JavdbMovieActorResource(
        javdb_id="actor-1",
        name="桥本有菜",
        alias_names=[],
        avatar_url="https://javdb.example/avatar.jpg",
    )

    class FailingResolver:
        def resolve(self, candidate_names):
            raise RuntimeError("cdn unavailable")

    provider = GfriendsAvatarJavdbProvider(CapturedProvider(kwargs={}, actors=[actor]), FailingResolver())

    detail = provider.get_movie_by_number("ABP-001")

    assert detail.actors[0].avatar_url == "https://javdb.example/avatar.jpg"
