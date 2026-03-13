from datetime import datetime

from src.model import Actor, Movie, MovieActor
from src.schema.metadata.javdb import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
    JavdbMovieListItemResource,
)
from src.service.catalog.subscribed_actor_movie_sync_service import SubscribedActorMovieSyncService


def _build_movie_list_item(javdb_id: str, movie_number: str) -> JavdbMovieListItemResource:
    return JavdbMovieListItemResource(
        javdb_id=javdb_id,
        movie_number=movie_number,
        title=movie_number,
        cover_image=None,
        release_date="2024-01-01",
        duration_minutes=120,
        score=0,
        watched_count=0,
        want_watch_count=0,
        comment_count=0,
        score_number=0,
        is_subscribed=False,
    )


def _build_movie_detail(actor: Actor, javdb_id: str, movie_number: str) -> JavdbMovieDetailResource:
    return JavdbMovieDetailResource(
        javdb_id=javdb_id,
        movie_number=movie_number,
        title=movie_number,
        cover_image=None,
        release_date="2024-01-01",
        duration_minutes=120,
        score=0,
        watched_count=0,
        want_watch_count=0,
        comment_count=0,
        score_number=0,
        is_subscribed=False,
        summary="",
        series_name=None,
        actors=[
            JavdbMovieActorResource(
                javdb_id=actor.javdb_id,
                javdb_type=actor.javdb_type,
                name=actor.name,
                avatar_url=None,
                gender=actor.gender,
            )
        ],
        tags=[],
        extra=None,
        plot_images=[],
    )


class FakeProvider:
    def __init__(self, pages, details):
        self.pages = pages
        self.details = details
        self.movie_detail_calls = []

    def get_actor_movies_by_javdb(self, actor_javdb_id: str, actor_type: int = 0, page: int = 1):
        return self.pages.get(page, [])

    def get_movie_by_javdb_id(self, movie_javdb_id: str):
        self.movie_detail_calls.append(movie_javdb_id)
        return self.details[movie_javdb_id]


class FakeImportService:
    def __init__(self, fail_on_javdb_id: str | None = None):
        self.fail_on_javdb_id = fail_on_javdb_id
        self.imported_javdb_ids = []

    def upsert_movie_from_javdb_detail(self, detail: JavdbMovieDetailResource):
        if self.fail_on_javdb_id == detail.javdb_id:
            raise RuntimeError("import failed")

        movie, _ = Movie.get_or_create(
            javdb_id=detail.javdb_id,
            defaults={
                "movie_number": detail.movie_number,
                "title": detail.title,
            },
        )
        if movie.movie_number != detail.movie_number or movie.title != detail.title:
            movie.movie_number = detail.movie_number
            movie.title = detail.title
            movie.save()

        for actor_resource in detail.actors:
            actor = Actor.get(Actor.javdb_id == actor_resource.javdb_id)
            MovieActor.get_or_create(movie=movie, actor=actor)

        self.imported_javdb_ids.append(detail.javdb_id)
        return movie


def test_sync_subscribed_actor_movies_runs_full_sync_for_unsynced_actor(app):
    actor = Actor.create(
        javdb_id="actor-1",
        javdb_type=0,
        name="三上悠亚",
        alias_name="三上悠亚",
        gender=1,
        is_subscribed=True,
    )
    provider = FakeProvider(
        pages={
            1: [
                _build_movie_list_item("movie-1", "ABP-001"),
                _build_movie_list_item("movie-2", "ABP-002"),
            ],
            2: [],
        },
        details={
            "movie-1": _build_movie_detail(actor, "movie-1", "ABP-001"),
            "movie-2": _build_movie_detail(actor, "movie-2", "ABP-002"),
        },
    )
    service = SubscribedActorMovieSyncService(
        provider=provider,
        import_service=FakeImportService(),
    )

    stats = service.sync_subscribed_actor_movies()
    actor = Actor.get_by_id(actor.id)

    assert stats == {
        "total_actors": 1,
        "success_actors": 1,
        "failed_actors": 0,
        "imported_movies": 2,
    }
    assert actor.subscribed_movies_synced_at is not None
    assert actor.subscribed_movies_full_synced_at is not None
    assert Movie.select().count() == 2
    assert MovieActor.select().where(MovieActor.actor == actor.id).count() == 2


def test_sync_subscribed_actor_movies_stops_incremental_sync_at_existing_actor_movie(app):
    synced_at = datetime(2024, 1, 1, 0, 0, 0)
    actor = Actor.create(
        javdb_id="actor-1",
        javdb_type=0,
        name="三上悠亚",
        alias_name="三上悠亚",
        gender=1,
        is_subscribed=True,
        subscribed_movies_synced_at=synced_at,
        subscribed_movies_full_synced_at=synced_at,
    )
    existing_movie = Movie.create(
        javdb_id="movie-old",
        movie_number="ABP-099",
        title="ABP-099",
    )
    MovieActor.create(movie=existing_movie, actor=actor)

    provider = FakeProvider(
        pages={
            1: [
                _build_movie_list_item("movie-new", "ABP-100"),
                _build_movie_list_item("movie-old", "ABP-099"),
                _build_movie_list_item("movie-skip", "ABP-098"),
            ]
        },
        details={
            "movie-new": _build_movie_detail(actor, "movie-new", "ABP-100"),
        },
    )
    service = SubscribedActorMovieSyncService(
        provider=provider,
        import_service=FakeImportService(),
    )

    stats = service.sync_subscribed_actor_movies()
    actor = Actor.get_by_id(actor.id)

    assert stats["imported_movies"] == 1
    assert provider.movie_detail_calls == ["movie-new"]
    assert actor.subscribed_movies_full_synced_at == synced_at
    assert actor.subscribed_movies_synced_at is not None
    assert actor.subscribed_movies_synced_at > synced_at
    assert Movie.get(Movie.javdb_id == "movie-new").movie_number == "ABP-100"


def test_sync_subscribed_actor_movies_does_not_advance_markers_when_actor_sync_fails(app):
    actor = Actor.create(
        javdb_id="actor-1",
        javdb_type=0,
        name="三上悠亚",
        alias_name="三上悠亚",
        gender=1,
        is_subscribed=True,
    )
    provider = FakeProvider(
        pages={1: [_build_movie_list_item("movie-1", "ABP-001")]},
        details={"movie-1": _build_movie_detail(actor, "movie-1", "ABP-001")},
    )
    service = SubscribedActorMovieSyncService(
        provider=provider,
        import_service=FakeImportService(fail_on_javdb_id="movie-1"),
    )

    stats = service.sync_subscribed_actor_movies()
    actor = Actor.get_by_id(actor.id)

    assert stats == {
        "total_actors": 1,
        "success_actors": 0,
        "failed_actors": 1,
        "imported_movies": 0,
    }
    assert actor.subscribed_movies_synced_at is None
    assert actor.subscribed_movies_full_synced_at is None


def test_sync_subscribed_actor_movies_keeps_full_sync_marker_after_resubscribe(app):
    full_synced_at = datetime(2024, 1, 1, 0, 0, 0)
    actor = Actor.create(
        javdb_id="actor-1",
        javdb_type=0,
        name="三上悠亚",
        alias_name="三上悠亚",
        gender=1,
        is_subscribed=False,
        subscribed_movies_synced_at=full_synced_at,
        subscribed_movies_full_synced_at=full_synced_at,
    )
    existing_movie = Movie.create(
        javdb_id="movie-old",
        movie_number="ABP-099",
        title="ABP-099",
    )
    MovieActor.create(movie=existing_movie, actor=actor)
    actor.is_subscribed = True
    actor.save()

    provider = FakeProvider(
        pages={1: [_build_movie_list_item("movie-old", "ABP-099")]},
        details={},
    )
    service = SubscribedActorMovieSyncService(
        provider=provider,
        import_service=FakeImportService(),
    )

    stats = service.sync_subscribed_actor_movies()
    actor = Actor.get_by_id(actor.id)

    assert stats["imported_movies"] == 0
    assert provider.movie_detail_calls == []
    assert actor.subscribed_movies_full_synced_at == full_synced_at
    assert actor.subscribed_movies_synced_at is not None
    assert actor.subscribed_movies_synced_at > full_synced_at
