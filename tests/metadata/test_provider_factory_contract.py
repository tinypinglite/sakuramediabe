from dataclasses import dataclass, field

import pytest
from sakuramedia_metadata_providers.models import JavdbMovieActorResource, JavdbMovieDetailResource

from src.config.config import settings
from src.metadata.factory import GfriendsAvatarJavdbProvider, build_dmm_provider, build_javdb_provider, build_missav_ranking_provider, build_missav_thumbnail_provider


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


@pytest.fixture(autouse=True)
def fixed_license_runtime(monkeypatch):
    monkeypatch.setattr(
        "src.metadata.factory.resolve_metadata_provider_license_runtime",
        lambda: type(
            "Runtime",
            (),
            {
                "as_provider_kwargs": lambda self: {
                    "version": "v1.2.3",
                    "state_path": "/data/config/provider-license-state.json",
                    "license_proxy": "http://license-proxy:7890",
                }
            },
        )(),
    )


def test_build_dmm_provider_passes_site_proxy_and_license_runtime(monkeypatch):
    captured = {}

    def fake_create_dmm_provider(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(settings.metadata, "proxy", "  http://site-proxy:7890  ")
    monkeypatch.setattr(settings.metadata, "dmm_proxy", None)
    monkeypatch.setattr("src.metadata.factory.create_dmm_provider", fake_create_dmm_provider)

    provider = build_dmm_provider()

    assert provider is not None
    assert captured == {
        "proxy": "http://site-proxy:7890",
        "version": "v1.2.3",
        "state_path": "/data/config/provider-license-state.json",
        "license_proxy": "http://license-proxy:7890",
    }


@pytest.mark.parametrize(
    ("use_metadata_proxy", "expected_provider_proxy", "expected_gfriends_proxy"),
    [
        (False, None, "http://site-proxy:7890"),
        (True, "http://site-proxy:7890", "http://site-proxy:7890"),
    ],
)
def test_build_javdb_provider_routes_site_proxy_and_license_runtime(
    monkeypatch,
    use_metadata_proxy,
    expected_provider_proxy,
    expected_gfriends_proxy,
):
    captured = {"resolver_proxy": None, "provider": None}

    class FakeResolver:
        def __init__(self, **kwargs):
            captured["resolver_proxy"] = kwargs["proxy"]

    def fake_create_javdb_provider(**kwargs):
        provider = CapturedProvider(kwargs=kwargs)
        captured["provider"] = provider
        return provider

    monkeypatch.setattr(settings.metadata, "proxy", "  http://site-proxy:7890  ")
    monkeypatch.setattr(settings.metadata, "dmm_proxy", None)
    monkeypatch.setattr("src.metadata.factory.GfriendsActorImageResolver", FakeResolver)
    monkeypatch.setattr("src.metadata.factory.create_javdb_provider", fake_create_javdb_provider)

    provider = build_javdb_provider(use_metadata_proxy=use_metadata_proxy)

    assert isinstance(provider, GfriendsAvatarJavdbProvider)
    assert captured["provider"].kwargs == {
        "host": settings.metadata.javdb_host,
        "proxy": expected_provider_proxy,
        "version": "v1.2.3",
        "state_path": "/data/config/provider-license-state.json",
        "license_proxy": "http://license-proxy:7890",
    }
    assert captured["resolver_proxy"] == expected_gfriends_proxy


def test_build_missav_providers_pass_site_proxy_and_license_runtime(monkeypatch):
    captured = {}

    def fake_create_thumbnail(**kwargs):
        captured["thumbnail"] = kwargs
        return object()

    def fake_create_ranking(**kwargs):
        captured["ranking"] = kwargs
        return object()

    monkeypatch.setattr("src.metadata.factory.create_missav_thumbnail_provider", fake_create_thumbnail)
    monkeypatch.setattr("src.metadata.factory.create_missav_ranking_provider", fake_create_ranking)
    monkeypatch.setattr(settings.metadata, "proxy", "  http://site-proxy:7890  ")
    monkeypatch.setattr(settings.metadata, "dmm_proxy", None)

    assert build_missav_thumbnail_provider() is not None
    assert build_missav_ranking_provider() is not None
    assert captured == {
        "thumbnail": {
            "proxy": "http://site-proxy:7890",
            "version": "v1.2.3",
            "state_path": "/data/config/provider-license-state.json",
            "license_proxy": "http://license-proxy:7890",
        },
        "ranking": {
            "proxy": "http://site-proxy:7890",
            "version": "v1.2.3",
            "state_path": "/data/config/provider-license-state.json",
            "license_proxy": "http://license-proxy:7890",
        },
    }


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
