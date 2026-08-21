from dataclasses import dataclass, field

import pytest

from src.config.config import settings
from src.metadata import factory as factory_module
from src.metadata._providers.javdb import JavdbProvider
from src.metadata._providers.models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
)
from src.metadata.factory import (
    GfriendsAvatarJavdbProvider,
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


def test_build_javdb_provider_never_uses_explicit_proxy():
    # JavDB provider 不接收任何显式代理：是否走代理由容器层
    # HTTP_PROXY / NO_PROXY 环境变量决定，与 config 无耦合。
    provider = build_javdb_provider()

    assert isinstance(provider, GfriendsAvatarJavdbProvider)
    assert provider.provider.host == settings.metadata.javdb_host
    # 显式代理配置已整体移除，client 上不应再出现 proxy 概念。
    assert not hasattr(provider.provider, "proxy")


def test_javdb_actor_gender_mapping_keeps_unknown_separate_from_female():
    provider = JavdbProvider("example.com")

    actors = provider._build_movie_actors(
        [
            {"id": "female", "name": "女性", "gender": 0},
            {"id": "male", "name": "男性", "gender": 1},
            {"id": "unknown", "name": "未知", "gender": None},
        ]
    )

    assert [actor.gender for actor in actors] == [1, 2, 0]


def test_build_javdb_provider_passes_account_credentials():
    # 账号不再属于宿主 metadata 配置，由排行榜插件显式注入；宿主默认构建无账号实例。
    provider = build_javdb_provider(
        username="user@example.com",
        password="secret",
    )

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


def test_gfriends_resolver_is_singleton_per_config_key():
    provider_a = build_javdb_provider()
    provider_b = build_javdb_provider()

    # 同一 (url, cdn, cache_path, ttl) 组合下应命中缓存返回同实例，
    # 让预热任务写入的内存 index 能被业务侧直接看到。
    assert provider_a.actor_image_resolver is provider_b.actor_image_resolver


def test_gfriends_resolver_cache_evicts_previous_config(monkeypatch):
    # 配置换代后旧实例必须被 evict，避免长期热更新累积无引用的 resolver 内存。
    monkeypatch.setattr(settings.metadata, "gfriends_filetree_url", "https://cdn.example/a/Filetree.json")
    build_javdb_provider()

    monkeypatch.setattr(settings.metadata, "gfriends_filetree_url", "https://cdn.example/b/Filetree.json")
    build_javdb_provider()

    assert len(factory_module._resolver_cache) == 1


def test_refresh_gfriends_filetree_delegates_to_resolver(monkeypatch):
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
