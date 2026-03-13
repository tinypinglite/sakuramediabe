from datetime import datetime

from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError
from src.model import Actor, Image, Media, Movie, MovieActor, MovieTag, Tag
from src.schema.catalog.actors import ActorListGender, ActorListSubscriptionStatus
from src.schema.metadata.javdb import JavdbMovieActorResource
from src.service.catalog.actor_service import ActorService
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError


def _capture_queries(test_db):
    queries: list[str] = []
    original_execute_sql = test_db.execute_sql

    def capture(sql, params=None, commit=None):
        queries.append(sql)
        return original_execute_sql(sql, params, commit)

    test_db.execute_sql = capture
    return queries, original_execute_sql


def _restore_queries(test_db, original_execute_sql):
    test_db.execute_sql = original_execute_sql


def _create_actor(name: str, javdb_id: str, **kwargs):
    payload = {
        "name": name,
        "javdb_id": javdb_id,
        "alias_name": kwargs.pop("alias_name", ""),
    }
    payload.update(kwargs)
    return Actor.create(**payload)


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def test_actor_service_list_actors_uses_database_pagination(
    app,
    test_db,
    build_signed_image_url,
):
    image = Image.create(origin="origin.jpg", small="small.jpg", medium="medium.jpg", large="large.jpg")
    _create_actor("河北彩花", "ActorA1", gender=1, is_subscribed=False)
    _create_actor(
        "三上悠亚",
        "ActorB2",
        alias_name="三上悠亚 / 鬼头桃菜",
        profile_image=image,
        is_subscribed=True,
        gender=1,
    )
    _create_actor("鬼头桃菜", "ActorC3", alias_name="三上悠亚 / 鬼头桃菜", is_subscribed=True, gender=1)
    _create_actor("天使萌", "ActorD4", gender=2, is_subscribed=True)

    queries, original_execute_sql = _capture_queries(test_db)

    try:
        response = ActorService.list_actors(
            gender=ActorListGender.FEMALE,
            subscription_status=ActorListSubscriptionStatus.SUBSCRIBED,
            page=2,
            page_size=1,
        )
    finally:
        _restore_queries(test_db, original_execute_sql)

    assert response.model_dump() == {
        "items": [
            {
                "id": 3,
                "javdb_id": "ActorC3",
                "name": "鬼头桃菜",
                "alias_name": "三上悠亚 / 鬼头桃菜",
                "profile_image": None,
                "is_subscribed": True,
            }
        ],
        "page": 2,
        "page_size": 1,
        "total": 2,
    }
    actor_queries = [sql for sql in queries if 'FROM "actor"' in sql]

    assert len(actor_queries) == 2
    assert any("LIMIT" in sql and "OFFSET" in sql for sql in actor_queries)


def test_actor_service_list_actors_all_gender_includes_unknown_gender(app):
    female_actor = _create_actor("河北彩花", "ActorA1", gender=1, is_subscribed=False)
    unknown_actor = _create_actor("未知演员", "ActorA2", gender=0, is_subscribed=False)
    male_actor = _create_actor("森林原人", "ActorA3", gender=2, is_subscribed=False)

    response = ActorService.list_actors(
        gender=ActorListGender.ALL,
        subscription_status=ActorListSubscriptionStatus.ALL,
    )

    assert [item.id for item in response.items] == [
        female_actor.id,
        unknown_actor.id,
        male_actor.id,
    ]
    assert response.total == 3


def test_actor_service_get_actor_tags_uses_tag_query_without_loading_movie_ids(app, test_db):
    actor = _create_actor("三上悠亚", "ActorA1")
    first_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    second_movie = _create_movie("ABC-002", "MovieB2", title="Movie 2")
    drama = Tag.create(name="剧情")
    uniform = Tag.create(name="制服")
    MovieActor.create(movie=first_movie, actor=actor)
    MovieActor.create(movie=second_movie, actor=actor)
    MovieTag.create(movie=first_movie, tag=drama)
    MovieTag.create(movie=second_movie, tag=drama)
    MovieTag.create(movie=second_movie, tag=uniform)

    queries, original_execute_sql = _capture_queries(test_db)

    try:
        response = ActorService.get_actor_tags(actor.id)
    finally:
        _restore_queries(test_db, original_execute_sql)

    assert sorted(
        [item.model_dump() for item in response],
        key=lambda item: item["tag_id"],
    ) == [
        {"tag_id": drama.id, "name": "剧情"},
        {"tag_id": uniform.id, "name": "制服"},
    ]
    standalone_movie_id_queries = [
        sql for sql in queries if 'SELECT "t1"."id" FROM "movie"' in sql and 'FROM "tag"' not in sql
    ]

    assert standalone_movie_id_queries == []


def test_actor_service_get_actor_years_uses_database_distinct(app, test_db):
    actor = _create_actor("三上悠亚", "ActorA1")
    older_movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        release_date=datetime(2023, 1, 2, 3, 4, 5),
    )
    newer_movie = _create_movie(
        "ABC-002",
        "MovieB2",
        title="Movie 2",
        release_date=datetime(2024, 2, 3, 4, 5, 6),
    )
    same_year_movie = _create_movie(
        "ABC-003",
        "MovieC3",
        title="Movie 3",
        release_date=datetime(2024, 5, 6, 7, 8, 9),
    )
    MovieActor.create(movie=older_movie, actor=actor)
    MovieActor.create(movie=newer_movie, actor=actor)
    MovieActor.create(movie=same_year_movie, actor=actor)

    queries, original_execute_sql = _capture_queries(test_db)

    try:
        response = ActorService.get_actor_years(actor.id)
    finally:
        _restore_queries(test_db, original_execute_sql)

    assert [item.year for item in response] == [2024, 2023]
    year_queries = [sql for sql in queries if 'FROM "movie"' in sql]

    assert len(year_queries) == 1
    assert "DISTINCT" in year_queries[0]
    assert "strftime" in year_queries[0]


def test_actor_service_get_actor_movies_sets_can_play_by_valid_media(app):
    actor = _create_actor("三上悠亚", "ActorA1")
    playable_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    not_playable_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    MovieActor.create(movie=playable_movie, actor=actor)
    MovieActor.create(movie=not_playable_movie, actor=actor)
    Media.create(movie=playable_movie, path="/library/main/abc-001.mp4", valid=True)
    Media.create(movie=not_playable_movie, path="/library/main/abc-002.mp4", valid=False)

    response = ActorService.get_actor_movies(actor.id, page=1, page_size=20)

    assert response.model_dump()["items"] == [
        {
            "javdb_id": "MovieA1",
            "movie_number": "ABC-001",
            "title": "Movie 1",
            "cover_image": None,
            "can_play": True,
        },
        {
            "javdb_id": "MovieA2",
            "movie_number": "ABC-002",
            "title": "Movie 2",
            "cover_image": None,
            "can_play": False,
        },
    ]


def test_stream_search_actor_uses_catalog_import_service(app, test_db, monkeypatch):
    called = {"upsert": 0}

    class FakeProvider:
        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name=f"{actor_name}-1",
                    avatar_url="https://c0.jdbstatic.com/avatars/a1.jpg",
                    gender=1,
                ),
                JavdbMovieActorResource(
                    javdb_id="ActorA2",
                    name=f"{actor_name}-2",
                    avatar_url=None,
                    gender=1,
                ),
            ]

    class FakeCatalogImportService:
        def upsert_actor_from_javdb_resource(self, actor_resource):
            called["upsert"] += 1
            return Actor.create(
                javdb_id=actor_resource.javdb_id,
                name=actor_resource.name,
                alias_name=actor_resource.name,
                gender=actor_resource.gender,
            )

    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(ActorService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    events = list(ActorService.stream_search_and_upsert_actor_from_javdb("三上悠亚"))

    assert called["upsert"] == 2
    assert [event for event, _ in events] == [
        "search_started",
        "actor_found",
        "upsert_started",
        "image_download_started",
        "image_download_finished",
        "image_download_started",
        "image_download_finished",
        "upsert_finished",
        "completed",
    ]
    assert events[1][1]["total"] == 2
    assert len(events[1][1]["actors"]) == 2
    assert events[-2][1] == {
        "total": 2,
        "created_count": 2,
        "already_exists_count": 0,
        "failed_count": 0,
    }
    assert events[-1][1]["success"] is True
    assert len(events[-1][1]["actors"]) == 2
    assert events[-1][1]["failed_items"] == []


def test_stream_search_actor_updates_avatar_with_catalog_import_service(app, test_db, monkeypatch, tmp_path):
    class FakeProvider:
        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name=actor_name,
                    avatar_url="https://c0.jdbstatic.com/avatars/a.jpg",
                    gender=1,
                )
            ]

    def fake_downloader(image_url, target_path):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"fake-image-content")

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(
        ActorService,
        "_build_catalog_import_service",
        lambda: CatalogImportService(image_downloader=fake_downloader),
    )

    events = list(ActorService.stream_search_and_upsert_actor_from_javdb("三上悠亚"))

    actor = Actor.get(Actor.javdb_id == "ActorA1")
    assert actor.profile_image_id is not None
    assert [event for event, _ in events] == [
        "search_started",
        "actor_found",
        "upsert_started",
        "image_download_started",
        "image_download_finished",
        "upsert_finished",
        "completed",
    ]


def test_stream_search_actor_preserves_existing_subscription_state(app, test_db, monkeypatch, tmp_path):
    synced_at = datetime(2026, 3, 10, 9, 0, 0)
    full_synced_at = datetime(2026, 3, 8, 9, 0, 0)
    _create_actor(
        "Old Name",
        "ActorA1",
        alias_name="Old Alias",
        is_subscribed=True,
        subscribed_movies_synced_at=synced_at,
        subscribed_movies_full_synced_at=full_synced_at,
    )

    class FakeProvider:
        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name=actor_name,
                    avatar_url=None,
                    gender=1,
                )
            ]

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(
        ActorService,
        "_build_catalog_import_service",
        lambda: CatalogImportService(image_downloader=lambda url, target_path: target_path.write_bytes(b"img")),
    )

    events = list(ActorService.stream_search_and_upsert_actor_from_javdb("三上悠亚"))

    actor = Actor.get(Actor.javdb_id == "ActorA1")
    assert actor.is_subscribed is True
    assert actor.subscribed_movies_synced_at == synced_at
    assert actor.subscribed_movies_full_synced_at == full_synced_at
    assert events[-1][1]["success"] is True
    assert events[-1][1]["actors"][0]["is_subscribed"] is True


def test_stream_search_actor_maps_image_download_failure(app, test_db, monkeypatch):
    class FakeProvider:
        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name=f"{actor_name}-1",
                    avatar_url="https://c0.jdbstatic.com/avatars/a1.jpg",
                    gender=1,
                ),
                JavdbMovieActorResource(
                    javdb_id="ActorA2",
                    name=f"{actor_name}-2",
                    avatar_url="https://c0.jdbstatic.com/avatars/a2.jpg",
                    gender=1,
                ),
            ]

    class FakeCatalogImportService:
        def upsert_actor_from_javdb_resource(self, actor_resource):
            if actor_resource.javdb_id == "ActorA2":
                raise ImageDownloadError("download_failed")
            return Actor.create(
                javdb_id=actor_resource.javdb_id,
                name=actor_resource.name,
                alias_name=actor_resource.name,
                gender=actor_resource.gender,
            )

    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(ActorService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    events = list(ActorService.stream_search_and_upsert_actor_from_javdb("三上悠亚"))

    assert [event for event, _ in events] == [
        "search_started",
        "actor_found",
        "upsert_started",
        "image_download_started",
        "image_download_finished",
        "image_download_started",
        "upsert_finished",
        "completed",
    ]
    assert events[-1][1]["success"] is True
    assert [actor["javdb_id"] for actor in events[-1][1]["actors"]] == ["ActorA1"]
    assert events[-1][1]["failed_items"] == [
        {
            "javdb_id": "ActorA2",
            "reason": "image_download_failed",
            "detail": "download_failed",
        }
    ]


def test_stream_search_actor_returns_not_found_when_provider_misses(app, test_db, monkeypatch):
    class FakeProvider:
        def search_actors(self, actor_name: str):
            raise MetadataNotFoundError("actor", actor_name)

    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider())

    events = list(ActorService.stream_search_and_upsert_actor_from_javdb("unknown"))

    assert [event for event, _ in events] == [
        "search_started",
        "completed",
    ]
    assert events[-1][1]["reason"] == "actor_not_found"


def test_stream_search_actor_returns_internal_error_when_all_candidates_fail(app, test_db, monkeypatch):
    class FakeProvider:
        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name=actor_name,
                    avatar_url="https://c0.jdbstatic.com/avatars/a1.jpg",
                    gender=1,
                )
            ]

    class FakeCatalogImportService:
        def upsert_actor_from_javdb_resource(self, actor_resource):
            raise RuntimeError("db error")

    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(ActorService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    events = list(ActorService.stream_search_and_upsert_actor_from_javdb("三上悠亚"))

    assert [event for event, _ in events] == [
        "search_started",
        "actor_found",
        "upsert_started",
        "image_download_started",
        "upsert_finished",
        "completed",
    ]
    assert events[-1][1]["success"] is False
    assert events[-1][1]["reason"] == "internal_error"
    assert events[-1][1]["actors"] == []
