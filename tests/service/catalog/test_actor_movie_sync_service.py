from src.config.config import settings
from src.model import Actor, Movie, MovieActor
from src.plugins import (
    PluginActorMoviePage,
    PluginActorMovieQuery,
    PluginActorRef,
    PluginMetadataSource,
)
from src.service.catalog.metadata_source_service import MetadataSourceService
from src.service.catalog.subscribed_actor_movie_sync_service import (
    SubscribedActorMovieSyncService,
)


class _Reporter:
    def __init__(self):
        self.progress = []

    def emit(self, **kwargs):
        self.progress.append(kwargs)


class _FakeSource:
    def __init__(self, pages):
        self.pages = pages
        self.queries = []

    def fetch(self, query):
        self.queries.append(query)
        result = self.pages.get((query.actor.actor_id, query.cursor))
        if isinstance(result, Exception):
            raise result
        return result


def _install_sources(monkeypatch, sources):
    monkeypatch.setattr(
        MetadataSourceService,
        "sources",
        tuple(
            (plugin_id, plugin_id, {}, source)
            for plugin_id, source in sources
        ),
    )
    monkeypatch.setattr(
        settings.plugins,
        "enabled",
        [plugin_id for plugin_id, _source in sources],
    )


def _source(pages):
    fake = _FakeSource(pages)
    return (
        PluginMetadataSource(
            fetch_movie=lambda _number: None,
            fetch_actor_movies=fake.fetch,
        ),
        fake,
    )


def _plain_source():
    return PluginMetadataSource(fetch_movie=lambda _number: None)


def _install_import(monkeypatch, *, failed_numbers=()):
    calls = []

    def fake_import(movie_number, _import_service, *, force_subscribed=False):
        calls.append((movie_number, force_subscribed))
        if movie_number in failed_numbers:
            raise RuntimeError("import failed")
        movie = Movie.create(movie_number=movie_number, title=movie_number)
        return movie, True

    monkeypatch.setattr(
        MetadataSourceService,
        "import_by_number",
        staticmethod(fake_import),
    )
    return calls


def test_actor_movie_page_cleans_and_deduplicates_numbers():
    page = PluginActorMoviePage(
        movie_numbers=[" ABC-001 ", "", "abc-001", "ABC-002"],
        next_cursor=" page-2 ",
    )

    assert page.movie_numbers == ["ABC-001", "ABC-002"]
    assert page.next_cursor == "page-2"
    assert PluginActorMoviePage(movie_numbers=[], next_cursor="").next_cursor is None
    assert (
        PluginActorMovieQuery(
            actor=PluginActorRef(actor_id=1, name="演员"),
            cursor="",
        ).cursor
        is None
    )


def test_actor_movie_capability_is_optional(monkeypatch):
    assert _plain_source().fetch_actor_movies is None
    capable, _fake = _source({})
    assert capable.fetch_actor_movies is not None

    _install_sources(
        monkeypatch,
        [("plain", _plain_source()), ("capable", capable)],
    )

    assert [
        entry[0] for entry in MetadataSourceService.enabled_actor_movie_sources()
    ] == ["capable"]


def test_sync_uses_all_capable_sources_and_links_imported_movies(
    test_db, monkeypatch
):
    actor = Actor.create(name="演员", is_subscribed=True, gender=1)
    first, first_fake = _source(
        {
            (actor.id, None): PluginActorMoviePage(
                movie_numbers=["ABC-001", "abc-001", "ABC-002"],
                next_cursor="page-2",
            ),
            (actor.id, "page-2"): PluginActorMoviePage(
                movie_numbers=["ABC-003"],
            ),
        }
    )
    second, second_fake = _source(
        {
            (actor.id, None): PluginActorMoviePage(
                movie_numbers=["ABC-002", "ABC-004"],
            ),
        }
    )
    _install_sources(
        monkeypatch,
        [("first", first), ("second", second)],
    )
    import_calls = _install_import(monkeypatch)
    reporter = _Reporter()

    result = SubscribedActorMovieSyncService().run(reporter=reporter)

    assert result["status"] == "ok"
    assert result["discovered_movies"] == 4
    assert result["imported_movies"] == 4
    assert result["source_calls"] == 2
    assert result["source_failures"] == 0
    assert sorted(import_calls) == [
        ("ABC-001", False),
        ("ABC-002", False),
        ("ABC-003", False),
        ("ABC-004", False),
    ]
    assert Movie.select().count() == 4
    assert MovieActor.select().count() == 4
    assert [query.mode for query in first_fake.queries] == [
        "incremental",
        "incremental",
    ]
    assert [query.mode for query in second_fake.queries] == ["incremental"]
    assert reporter.progress


def test_sync_passes_full_mode_and_keeps_source_failures_isolated(
    test_db, monkeypatch
):
    actor = Actor.create(name="演员", is_subscribed=True, gender=1)
    broken, broken_fake = _source(
        {
            (actor.id, None): RuntimeError("source down"),
        }
    )
    good, good_fake = _source(
        {
            (actor.id, None): PluginActorMoviePage(
                movie_numbers=["ABC-010"],
            ),
        }
    )
    _install_sources(
        monkeypatch,
        [("broken", broken), ("good", good)],
    )
    import_calls = _install_import(monkeypatch)

    result = SubscribedActorMovieSyncService().run(
        reporter=_Reporter(),
        params={"mode": "full"},
    )

    assert result["mode"] == "full"
    assert result["source_failures"] == 1
    assert result["imported_movies"] == 1
    assert import_calls == [("ABC-010", False)]
    assert [query.mode for query in broken_fake.queries] == ["full"]
    assert [query.mode for query in good_fake.queries] == ["full"]


def test_sync_counts_unresolved_and_failed_actors(test_db, monkeypatch):
    unresolved = Actor.create(name="无法识别", is_subscribed=True, gender=1)
    failed = Actor.create(name="来源失败", is_subscribed=True, gender=1)
    source, _fake = _source(
        {
            (unresolved.id, None): None,
            (failed.id, None): RuntimeError("source down"),
        }
    )
    _install_sources(monkeypatch, [("source", source)])

    result = SubscribedActorMovieSyncService().run(reporter=_Reporter())

    assert result["unresolved_actors"] == 1
    assert result["failed_actors"] == 1
    assert result["source_failures"] == 1
    assert result["imported_movies"] == 0


def test_sync_keeps_partial_page_results_when_cursor_repeats(
    test_db, monkeypatch
):
    actor = Actor.create(name="演员", is_subscribed=True, gender=1)
    source, _fake = _source(
        {
            (actor.id, None): PluginActorMoviePage(
                movie_numbers=["ABC-020"],
                next_cursor="same",
            ),
            (actor.id, "same"): PluginActorMoviePage(
                movie_numbers=["ABC-021"],
                next_cursor="same",
            ),
        }
    )
    _install_sources(monkeypatch, [("source", source)])
    import_calls = _install_import(monkeypatch)

    result = SubscribedActorMovieSyncService().run(reporter=_Reporter())

    assert result["source_failures"] == 1
    assert result["imported_movies"] == 2
    assert import_calls == [("ABC-020", False), ("ABC-021", False)]


def test_sync_links_existing_movie_but_respects_manual_actor_owner(
    test_db, monkeypatch
):
    actor = Actor.create(name="演员", is_subscribed=True, gender=1)
    existing = Movie.create(movie_number="ABC-030", title="existing")
    manual = Movie.create(
        movie_number="ABC-031",
        title="manual",
        field_owners={"actors": "host:manual"},
    )
    source, _fake = _source(
        {
            (actor.id, None): PluginActorMoviePage(
                movie_numbers=["ABC-030", "ABC-031"],
            ),
        }
    )
    _install_sources(monkeypatch, [("source", source)])
    import_calls = _install_import(monkeypatch)

    result = SubscribedActorMovieSyncService().run(reporter=_Reporter())

    assert result["existing_movies"] == 2
    assert result["imported_movies"] == 0
    assert import_calls == []
    assert MovieActor.select().where(
        MovieActor.movie == existing,
        MovieActor.actor == actor,
    ).exists()
    assert not MovieActor.select().where(
        MovieActor.movie == manual,
        MovieActor.actor == actor,
    ).exists()


def test_sync_failed_movie_does_not_block_other_movies(test_db, monkeypatch):
    actor = Actor.create(name="演员", is_subscribed=True, gender=1)
    source, _fake = _source(
        {
            (actor.id, None): PluginActorMoviePage(
                movie_numbers=["ABC-040", "ABC-041"],
            ),
        }
    )
    _install_sources(monkeypatch, [("source", source)])
    _install_import(monkeypatch, failed_numbers={"ABC-040"})

    result = SubscribedActorMovieSyncService().run(reporter=_Reporter())

    assert result["failed_movies"] == 1
    assert result["imported_movies"] == 1
    assert Movie.get_or_none(Movie.movie_number == "ABC-041") is not None


def test_sync_skips_unsubscribed_actors_but_includes_all_genders(
    test_db, monkeypatch
):
    subscribed = Actor.create(name="订阅女优", is_subscribed=True, gender=1)
    subscribed_male = Actor.create(name="订阅男优", is_subscribed=True, gender=2)
    Actor.create(name="未订阅女优", is_subscribed=False, gender=1)
    source, fake = _source(
        {
            (subscribed.id, None): PluginActorMoviePage(
                movie_numbers=["ABC-050"],
            ),
        }
    )
    _install_sources(monkeypatch, [("source", source)])
    _install_import(monkeypatch)

    result = SubscribedActorMovieSyncService().run(reporter=_Reporter())

    assert result["total_actors"] == 2
    assert {query.actor.actor_id for query in fake.queries} == {
        subscribed.id,
        subscribed_male.id,
    }


def test_sync_skips_when_no_capable_source(test_db, monkeypatch):
    Actor.create(name="演员", is_subscribed=True, gender=1)
    _install_sources(monkeypatch, [("plain", _plain_source())])

    result = SubscribedActorMovieSyncService().run(reporter=_Reporter())

    assert result["status"] == "skipped"
    assert result["reason"] == "no_actor_movie_source"
    assert result["imported_movies"] == 0
