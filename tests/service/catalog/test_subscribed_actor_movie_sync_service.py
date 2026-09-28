from datetime import datetime
from types import SimpleNamespace

from src.metadata._providers.models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
)
from src.model import Actor, Movie, MovieActor
from src.service.catalog.actor_merge_service import ActorMergeService
from src.service.catalog.subscribed_actor_movie_sync_service import (
    SubscribedActorMovieSyncService,
)


def _build_detail(
    javdb_id: str,
    movie_number: str,
    *,
    actor_javdb_id: str | None = None,
    actor_name: str = "演员",
) -> JavdbMovieDetailResource:
    actors = []
    if actor_javdb_id is not None:
        actors.append(JavdbMovieActorResource(javdb_id=actor_javdb_id, name=actor_name))
    return JavdbMovieDetailResource(
        javdb_id=javdb_id,
        movie_number=movie_number,
        title=movie_number,
        summary="",
        duration_minutes=120,
        release_date="2024-01-01",
        score=0,
        score_number=0,
        watched_count=0,
        want_watch_count=0,
        comment_count=0,
        actors=actors,
        tags=[],
    )


class _FakeProvider:
    def __init__(self, pages, details):
        self.pages = pages
        self.details = details
        self.actor_calls: list[str] = []

    def get_actor_movies_by_javdb(self, *, actor_javdb_id: str, actor_type, page: int):
        self.actor_calls.append(actor_javdb_id)
        return self.pages.get((actor_javdb_id, page), [])

    def get_movie_by_javdb_id(self, javdb_id: str):
        return self.details[javdb_id]


def test_sync_covers_merged_source_javdb_ids(test_db):
    target = Actor.create(
        javdb_id="sync-canon",
        name="新名",
        is_subscribed=True,
        subscribed_at=datetime(2024, 1, 1, 8, 0, 0),
    )
    source = Actor.create(javdb_id="sync-source", name="旧名")
    ActorMergeService.merge_actors(target.id, [source.id])
    provider = _FakeProvider(
        pages={
            ("sync-canon", 1): [SimpleNamespace(javdb_id="SYNC-1")],
            ("sync-source", 1): [SimpleNamespace(javdb_id="SYNC-2")],
        },
        details={
            "SYNC-1": _build_detail(
                "SYNC-1", "SYNC-1", actor_javdb_id="sync-canon", actor_name="新名"
            ),
            "SYNC-2": _build_detail(
                "SYNC-2", "SYNC-2", actor_javdb_id="sync-source", actor_name="旧名"
            ),
        },
    )

    stats = SubscribedActorMovieSyncService(provider=provider).sync_subscribed_actor_movies()

    assert stats["imported_movies"] == 2
    assert "sync-canon" in provider.actor_calls
    assert "sync-source" in provider.actor_calls
    assert MovieActor.select().where(MovieActor.actor == target.id).count() == 2
    assert Movie.select().where(Movie.javdb_id.in_(["SYNC-1", "SYNC-2"])).count() == 2


def test_sync_incremental_continues_to_merged_source_after_existing_hit(test_db):
    target = Actor.create(
        javdb_id="sync-canon",
        name="新名",
        is_subscribed=True,
        subscribed_at=datetime(2024, 1, 1, 8, 0, 0),
        subscribed_movies_full_synced_at=datetime(2024, 2, 1, 8, 0, 0),
    )
    source = Actor.create(javdb_id="sync-source", name="旧名")
    ActorMergeService.merge_actors(target.id, [source.id])
    persisted = Actor.get_by_id(target.id)
    persisted.subscribed_movies_full_synced_at = datetime(2024, 2, 1, 8, 0, 0)
    persisted.save(only=[Actor.subscribed_movies_full_synced_at])

    existing_movie = Movie.create(
        movie_number="SYNC-OLD", javdb_id="SYNC-OLD", title="SYNC-OLD"
    )
    MovieActor.create(movie=existing_movie, actor=target)
    provider = _FakeProvider(
        pages={
            ("sync-canon", 1): [SimpleNamespace(javdb_id="SYNC-OLD")],
            ("sync-source", 1): [SimpleNamespace(javdb_id="SYNC-NEW")],
        },
        details={
            "SYNC-NEW": _build_detail(
                "SYNC-NEW", "SYNC-NEW", actor_javdb_id="sync-source", actor_name="旧名"
            )
        },
    )

    stats = SubscribedActorMovieSyncService(provider=provider).sync_subscribed_actor_movies()

    assert stats["imported_movies"] == 1
    imported = Movie.get_or_none(Movie.javdb_id == "SYNC-NEW")
    assert imported is not None
    assert MovieActor.select().where(
        MovieActor.movie == imported, MovieActor.actor == target
    ).exists()
