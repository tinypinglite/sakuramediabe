import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from PIL import Image as PillowImage
from pydantic import ValidationError

from src.common.runtime_time import utc_now_for_db
from src.config.config import Plugins, settings
from src.metadata._providers.javdb import JavdbProvider
from src.metadata._providers.models import (
    JavdbMovieActor,
    JavdbMovieDetail,
)
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import (
    Actor,
    Image,
    Media,
    MediaLibrary,
    MediaProgress,
    Movie,
    MovieActor,
    MovieTag,
    Tag,
)
from src.plugins import (
    PluginExtension,
    PluginMetadataSource,
    PluginMovieMetadata,
    PluginRegistration,
)
from src.plugins.extensions.metadata import METADATA_SOURCE_EXTENSION_KEY
from src.plugins.loader import PluginLoadError, check_plugin_dir
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.catalog.metadata_source_service import (
    MetadataSourceError,
    MetadataSourceService,
)
from src.service.catalog.movie_interaction_sync_service import (
    MovieInteractionSyncService,
)
from src.service.catalog.movie_javdb_backfill_service import MovieJavdbBackfillService
from src.service.catalog.movie_metadata_refresh_service import (
    MovieMetadataRefreshService,
)
from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway
from src.service.catalog.movie_service import MovieService


def remote_detail(**changes):
    values = {
        "javdb_id": "real-id",
        "movie_number": "TEST-001",
        "title": "JavDB title",
        "summary": "",
        "duration_minutes": 0,
        "actors": [],
        "tags": [],
        "score": 0,
    }
    values.update(changes)
    return JavdbMovieDetail(**values)


@pytest.fixture
def metadata_env(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.plugins, "root_dir", str(tmp_path / "plugins"))
    monkeypatch.setattr(settings.plugins, "enabled", ["metadata_one"])
    monkeypatch.setattr(
        settings.media, "import_image_root_path", str(tmp_path / "assets")
    )
    monkeypatch.setattr(MetadataSourceService, "sources", ())
    provider = SimpleNamespace(
        get_movie_by_number=Mock(
            side_effect=MetadataNotFoundError("movie", "TEST-001")
        ),
        search_actors=Mock(return_value=[]),
    )

    def image_download(_url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        PillowImage.new("RGB", (600, 400), "blue").save(path, format="PNG")

    service = CatalogImportService(image_downloader=image_download)
    return SimpleNamespace(
        provider=provider, service=service, root=tmp_path, download=image_download
    )


def delivery(env, plugin_id="metadata_one", request="one", **changes):
    folder = env.root / "plugins" / plugin_id / "data" / "metadata-tmp" / request
    folder.mkdir(parents=True, exist_ok=True)
    cover = folder / "cover.png"
    PillowImage.new("RGB", (600, 400), "red").save(cover)
    values = {
        "movie_number": "TEST-001",
        "title": "Plugin title",
        "release_date": "2026-09-01",
        "duration_minutes": 120,
        "cover_image_path": str(cover),
        "summary": "Plugin summary",
        "tags": [" tag ", "tag", ""],
        "source_url": "https://example.com/movie",
    }
    values.update(changes)
    return PluginMovieMetadata.model_validate(values)


def register_sources(monkeypatch, callbacks):
    monkeypatch.setattr(settings.plugins, "enabled", list(callbacks))
    MetadataSourceService.register(
        tuple(
            PluginRegistration(
                plugin_id=key,
                display_name=key,
                version="1.0",
                extensions=(
                    PluginExtension(
                        key=METADATA_SOURCE_EXTENSION_KEY,
                        data=PluginMetadataSource(fetch_movie=callback),
                    ),
                ),
            )
            for key, callback in callbacks.items()
        )
    )


def import_plugin(env, monkeypatch, **changes):
    detail = delivery(env, **changes)
    register_sources(monkeypatch, {"metadata_one": Mock(return_value=detail)})
    movie, created = MetadataSourceService.import_by_number(
        "TEST-001", env.provider, env.service
    )
    assert created
    return movie


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"release_date": None},
        {"release_date": "2026-02-30"},
        {"duration_minutes": 0},
        {"duration_minutes": True},
        {"cover_image_path": " "},
        {"javdb_id": "fake"},
        {"score": 9},
        {"score_number": 3},
        {"watched_count": 3},
        {"want_watch_count": 2},
        {"comment_count": 1},
    ],
)
def test_contract_rejects_missing_metadata_and_host_fields(changes):
    values = {
        "movie_number": "TEST-001",
        "title": "title",
        "release_date": "2026-09-01",
        "duration_minutes": 120,
        "cover_image_path": "/example.png",
    }
    with pytest.raises(ValidationError):
        PluginMovieMetadata.model_validate(values | changes)


def test_javdb_found_and_request_error_never_call_plugins(metadata_env, monkeypatch):
    callback = Mock()
    register_sources(monkeypatch, {"metadata_one": callback})
    provider = metadata_env.provider
    provider.get_movie_by_number.side_effect = None
    provider.get_movie_by_number.return_value = remote_detail()
    with MetadataSourceService.fetch("TEST-001", provider) as (detail, source):
        assert detail.javdb_id == "real-id" and source is None
    provider.get_movie_by_number.side_effect = MetadataRequestError(
        "GET", "https://example.com", "timeout"
    )
    with (
        pytest.raises(MetadataRequestError),
        MetadataSourceService.fetch("TEST-001", provider),
    ):
        pytest.fail("request errors cannot fall back")
    callback.assert_not_called()


def test_invalid_result_continues_in_order_and_cleans_delivery(
    metadata_env, monkeypatch
):
    bad = delivery(metadata_env, movie_number="OTHER-002")
    good = delivery(metadata_env, "metadata_two")
    third = Mock()
    register_sources(
        monkeypatch,
        {
            "metadata_one": Mock(return_value=bad),
            "metadata_two": Mock(return_value=good),
            "metadata_three": third,
        },
    )
    with MetadataSourceService.fetch("TEST-001", metadata_env.provider) as (
        detail,
        source,
    ):
        assert detail == good
        assert source["plugin_id"] == "metadata_two"
        assert not Path(bad.cover_image_path).exists()
        assert Path(good.cover_image_path).exists()
    assert not Path(good.cover_image_path).exists()
    third.assert_not_called()


def test_all_not_found_is_different_from_plugin_failure(metadata_env, monkeypatch):
    register_sources(monkeypatch, {"metadata_one": Mock(return_value=None)})
    with (
        pytest.raises(MetadataNotFoundError),
        MetadataSourceService.fetch("TEST-001", metadata_env.provider),
    ):
        pass
    register_sources(
        monkeypatch, {"metadata_one": Mock(side_effect=RuntimeError("broken"))}
    )
    with (
        pytest.raises(MetadataSourceError, match="metadata_one"),
        MetadataSourceService.fetch("TEST-001", metadata_env.provider),
    ):
        pass


def test_outside_and_symlink_paths_are_not_imported_or_deleted(
    metadata_env, monkeypatch
):
    detail = delivery(metadata_env)
    outside = metadata_env.root / "outside.png"
    PillowImage.new("RGB", (10, 10)).save(outside)
    Path(detail.cover_image_path).unlink()
    Path(detail.cover_image_path).symlink_to(outside)
    register_sources(monkeypatch, {"metadata_one": Mock(return_value=detail)})
    with (
        pytest.raises(MetadataSourceError),
        MetadataSourceService.fetch("TEST-001", metadata_env.provider),
    ):
        pass
    assert outside.is_file()
    assert Movie.select().count() == 0


def test_import_zero_statistics_images_and_repeat_lookup(metadata_env, monkeypatch):
    movie = import_plugin(metadata_env, monkeypatch)
    assert (
        movie.javdb_id is None and movie.metadata_source["plugin_id"] == "metadata_one"
    )
    assert movie.field_owners == {}
    assert movie.release_date == date(2026, 9, 1)
    assert movie.duration_minutes == 120
    assert all(
        getattr(movie, field) == 0
        for field in MovieInteractionSyncService.INTERACTION_FIELDS
    )
    assert movie.javdb_next_check_at > utc_now_for_db()
    cover = metadata_env.root / "assets" / movie.cover_image.origin
    assert cover.is_file()
    assert not list((metadata_env.root / "plugins").rglob("cover.png"))
    assert Tag.select().count() == 1
    resource = MovieService.get_movie_detail(movie.movie_number)
    assert resource.javdb_id is None and resource.cover_image is not None
    assert "metadata-tmp" not in resource.model_dump_json()
    metadata_env.provider.get_movie_by_number.reset_mock()
    again, created = MetadataSourceService.import_by_number(
        movie.movie_number, metadata_env.provider, metadata_env.service
    )
    assert again.id == movie.id and not created
    metadata_env.provider.get_movie_by_number.assert_not_called()
    Movie.create(movie_number="OTHER-002", title="second plugin", javdb_id=None)
    assert Movie.select().where(Movie.javdb_id.is_null()).count() == 2


def test_actors_only_link_unique_exact_matches(metadata_env, monkeypatch):
    local = Actor.create(javdb_id="local", name="Local", alias_name="Alias / Other")
    Actor.create(javdb_id="same-one", name="Same")
    Actor.create(javdb_id="same-two", name="Same")
    metadata_env.provider.search_actors.side_effect = lambda name: {
        "Remote": [JavdbMovieActor(javdb_id="remote", name="Remote")],
        "Missing": [],
        "Fuzzy": [JavdbMovieActor(javdb_id="fuzzy", name="Fuzzy-ish")],
    }.get(name, [])
    movie = import_plugin(
        metadata_env,
        monkeypatch,
        actors=[
            {"name": "Alias"},
            {"name": "Remote"},
            {"name": "Missing"},
            {"name": "Same"},
            {"name": "Fuzzy"},
        ],
    )
    actors = {
        link.actor.javdb_id
        for link in MovieActor.select().where(MovieActor.movie == movie)
    }
    assert actors == {local.javdb_id, "remote"}
    assert "Missing" not in json.dumps(movie.metadata_source)
    assert not Actor.select().where(Actor.javdb_id == "fuzzy").exists()


def test_actor_request_failure_does_not_block_movie(metadata_env, monkeypatch):
    metadata_env.provider.search_actors.side_effect = RuntimeError("offline")
    movie = import_plugin(metadata_env, monkeypatch, actors=[{"name": "Missing"}])
    assert MovieActor.select().where(MovieActor.movie == movie).count() == 0


def test_backfill_preserves_identity_user_state_and_valid_missing_fields(
    metadata_env, monkeypatch
):
    movie = import_plugin(metadata_env, monkeypatch)
    MovieOwnershipGateway.update_host_manual([movie.id], {"title": "Manual title"})
    movie.is_subscribed = True
    movie.save(only=[Movie.is_subscribed])
    library = MediaLibrary.create(name="local", provider_key="local")
    media = Media.create(
        movie=movie, library=library, file_name="movie.mp4", valid=True
    )
    progress = MediaProgress.create(media=media, position_seconds=42)
    cover = movie.cover_image.origin
    old_id, number = movie.id, movie.movie_number
    detail = remote_detail(
        actors_available=False, tags_available=False, watched_count=5
    )
    result, created = metadata_env.service.import_movie_if_missing(detail)
    assert not created and result.id == old_id and result.movie_number == number
    assert result.javdb_id == "real-id" and result.javdb_next_check_at is None
    assert result.title == "Manual title" and result.summary == "Plugin summary"
    assert result.duration_minutes == 120 and result.release_date.date() == date(
        2026, 9, 1
    )
    assert result.is_subscribed and result.cover_image.origin == cover
    assert result.metadata_source["plugin_id"] == "metadata_one"
    assert result.watched_count == 5 and result.score == 0
    assert MovieTag.select().where(MovieTag.movie == result).count() == 1
    assert Media.get_by_id(media.id).movie.id == old_id
    assert MediaProgress.get_by_id(progress.id).position_seconds == 42
    again, created = metadata_env.service.import_movie_if_missing(detail)
    assert again.id == old_id and not created and Movie.select().count() == 1


def test_backfill_replaces_lists_and_images_after_preparation(
    metadata_env, monkeypatch
):
    movie = import_plugin(metadata_env, monkeypatch, actors=[])
    old_cover = metadata_env.root / "assets" / movie.cover_image.origin
    detail = remote_detail(
        cover_image="https://example.com/new.png",
        tags=[],
        actors=[
            JavdbMovieActor(javdb_id="new-actor", name="New actor"),
        ],
    )
    result = metadata_env.service.backfill_plugin_movie(movie, detail)
    new_cover = metadata_env.root / "assets" / result.cover_image.origin
    assert new_cover.is_file() and new_cover != old_cover and not old_cover.exists()
    assert MovieTag.select().where(MovieTag.movie == movie).count() == 0
    assert MovieActor.get(MovieActor.movie == movie).actor.javdb_id == "new-actor"


@pytest.mark.parametrize("case", ["replace", "shared", "missing", "rollback"])
def test_backfill_cleans_only_unreferenced_old_actor_images(
    metadata_env, monkeypatch, case
):
    movie = import_plugin(metadata_env, monkeypatch)
    service = metadata_env.service
    actor = service.upsert_actor_from_javdb_resource(
        JavdbMovieActor(
            javdb_id="existing-actor", name="Actor",
            avatar_url="https://example.com/old.png",
        )
    )
    old_image_id = actor.profile_image_id
    assets = metadata_env.root / "assets"
    old_path = assets / actor.profile_image.origin
    if case == "shared":
        Actor.create(javdb_id="other-actor", name="Other", profile_image=old_image_id)
    detail = remote_detail(actors=[
        JavdbMovieActor(
            javdb_id=actor.javdb_id, name=actor.name,
            avatar_url=None if case == "missing" else "https://example.com/new.png",
        )
    ])
    if case == "rollback":
        paths_before = {path for path in assets.rglob("*") if path.is_file()}
        monkeypatch.setattr(
            "src.service.catalog.catalog_import_service.MovieHeatService.update_single_movie_heat",
            Mock(side_effect=RuntimeError("rollback after image cleanup")),
        )
        with pytest.raises(RuntimeError, match="rollback after image cleanup"):
            service.backfill_plugin_movie(movie, detail)
        assert Movie.get_by_id(movie.id).javdb_id is None
        assert {path for path in assets.rglob("*") if path.is_file()} == paths_before
    else:
        service.backfill_plugin_movie(movie, detail)

    current = Actor.get_by_id(actor.id)
    if case in {"missing", "rollback"}:
        assert current.profile_image_id == old_image_id
    else:
        assert current.profile_image_id != old_image_id
    assert (assets / current.profile_image.origin).is_file()
    keep_old_image = case != "replace"
    assert Image.select().where(Image.id == old_image_id).exists() == keep_old_image
    assert old_path.is_file() == keep_old_image


def test_backfill_failure_preserves_old_data_and_files(metadata_env, monkeypatch):
    movie = import_plugin(metadata_env, monkeypatch)
    paths_before = {
        path for path in (metadata_env.root / "assets").rglob("*") if path.is_file()
    }
    detail = remote_detail(cover_image="https://example.com/new.png")
    monkeypatch.setattr(
        MovieOwnershipGateway,
        "update_host_unowned",
        Mock(side_effect=RuntimeError("rollback")),
    )
    with pytest.raises(RuntimeError, match="rollback"):
        metadata_env.service.backfill_plugin_movie(movie, detail)
    current = Movie.get_by_id(movie.id)
    assert current.javdb_id is None and current.title == "Plugin title"
    paths_after = {
        path for path in (metadata_env.root / "assets").rglob("*") if path.is_file()
    }
    assert paths_after == paths_before


def test_download_failure_does_not_bind_javdb_id(metadata_env, monkeypatch):
    movie = import_plugin(metadata_env, monkeypatch)
    monkeypatch.setattr(
        metadata_env.service.image_service,
        "image_downloader",
        Mock(side_effect=OSError("download")),
    )
    with pytest.raises(OSError):
        metadata_env.service.backfill_plugin_movie(
            movie, remote_detail(cover_image="https://example.com/new.png")
        )
    assert Movie.get_by_id(movie.id).javdb_id is None
    assert (metadata_env.root / "assets" / movie.cover_image.origin).is_file()


def test_javdb_conflicts_do_not_merge_or_modify_movie(metadata_env, monkeypatch):
    movie = import_plugin(metadata_env, monkeypatch)
    Movie.create(movie_number="OTHER-001", javdb_id="real-id", title="other")
    with pytest.raises(ValueError, match="其他"):
        metadata_env.service.import_movie_if_missing(remote_detail())
    with pytest.raises(ValueError, match="番号"):
        metadata_env.service.backfill_plugin_movie(
            movie, remote_detail(movie_number="OTHER-002")
        )
    assert Movie.get_by_id(movie.id).javdb_id is None
    assert Movie.select().count() == 2


def test_scheduler_only_processes_due_plugin_movies_without_plugin(
    metadata_env, monkeypatch
):
    movie = import_plugin(metadata_env, monkeypatch)
    Movie.update(javdb_next_check_at=utc_now_for_db() - timedelta(days=1)).where(
        Movie.id == movie.id
    ).execute()
    Movie.create(
        movie_number="LATER-001",
        title="later",
        metadata_source={"plugin_id": "removed"},
        javdb_next_check_at=utc_now_for_db() + timedelta(days=1),
    )
    Movie.create(movie_number="NORMAL-001", title="normal", javdb_id="normal")
    monkeypatch.setattr(settings.plugins, "enabled", [])
    MetadataSourceService.register(())
    metadata_env.provider.get_movie_by_number.side_effect = None
    metadata_env.provider.get_movie_by_number.return_value = remote_detail()
    metadata_env.provider.get_movie_by_number.reset_mock()
    service = MovieJavdbBackfillService(metadata_env.provider, metadata_env.service)
    reporter = SimpleNamespace(emit=Mock())
    stats = service.run(reporter=reporter)
    assert stats["candidate_movies"] == stats["succeeded_movies"] == 1
    metadata_env.provider.get_movie_by_number.assert_called_once_with("TEST-001")
    assert service.run(reporter=reporter)["candidate_movies"] == 0


@pytest.mark.parametrize(
    "error, counter",
    [
        (MetadataNotFoundError("movie", "TEST-001"), "not_found_movies"),
        (RuntimeError("offline"), "failed_movies"),
    ],
)
def test_scheduler_failures_use_same_fixed_interval(
    metadata_env, monkeypatch, error, counter
):
    movie = import_plugin(metadata_env, monkeypatch)
    Movie.update(javdb_next_check_at=utc_now_for_db() - timedelta(days=1)).where(
        Movie.id == movie.id
    ).execute()
    metadata_env.provider.get_movie_by_number.side_effect = error
    stats = MovieJavdbBackfillService(metadata_env.provider, metadata_env.service).run(
        reporter=SimpleNamespace(emit=Mock())
    )
    assert stats[counter] == 1
    current = Movie.get_by_id(movie.id)
    assert current.javdb_id is None
    assert current.javdb_next_check_at > utc_now_for_db() + timedelta(days=6)


def test_plugin_movie_reviews_and_statistics_do_not_request_javdb(
    metadata_env, monkeypatch
):
    movie = import_plugin(metadata_env, monkeypatch)
    factory = Mock(side_effect=AssertionError("must not request JavDB"))
    monkeypatch.setattr(
        "src.service.catalog.movie_service.build_javdb_provider", factory
    )
    assert MovieService.get_movie_reviews(movie.movie_number) == []
    assert movie.id not in MovieInteractionSyncService._candidate_ids()


def test_stream_and_manual_refresh_use_plugin_then_backfill(metadata_env, monkeypatch):
    detail = delivery(metadata_env)
    register_sources(monkeypatch, {"metadata_one": Mock(return_value=detail)})
    monkeypatch.setattr(
        "src.service.catalog.movie_metadata_refresh_service.build_javdb_provider",
        lambda: metadata_env.provider,
    )
    monkeypatch.setattr(
        MovieMetadataRefreshService,
        "_build_catalog_import_service",
        lambda: metadata_env.service,
    )
    events = list(
        MovieMetadataRefreshService.stream_search_and_upsert_movie_from_javdb(
            "TEST-001"
        )
    )
    assert events[-1][1]["success"]
    assert events[-1][1]["movies"][0]["javdb_id"] is None
    assert "metadata-tmp" not in json.dumps(events)
    metadata_env.provider.get_movie_by_number.side_effect = None
    metadata_env.provider.get_movie_by_number.return_value = remote_detail()
    result = MovieMetadataRefreshService.refresh_movie_metadata("TEST-001")
    assert result.javdb_id == "real-id"


def test_provider_preserves_missing_versus_empty_relations():
    provider = JavdbProvider("example.com")
    try:
        missing = provider._build_movie_detail(
            {"data": {"movie": {"id": "real", "number": "TEST-001"}}}
        )
        empty = provider._build_movie_detail(
            {
                "data": {
                    "movie": {
                        "id": "real",
                        "number": "TEST-001",
                        "actors": [],
                        "tags": [],
                    }
                }
            }
        )
        assert not missing.actors_available and not missing.tags_available
        assert empty.actors_available and empty.tags_available
        partial = provider._build_movie_detail(
            {
                "data": {
                    "movie": {
                        "id": "real",
                        "number": "TEST-001",
                        "actors": [None],
                        "tags": [None],
                    }
                }
            }
        )
        assert not partial.actors_available and not partial.tags_available
    finally:
        provider.client.close()


def test_manifest_must_declare_metadata_capability_version(tmp_path):
    folder = tmp_path / "metadata_one"
    folder.mkdir()
    manifest = {
        "plugin_id": "metadata_one",
        "display_name": "Metadata",
        "version": "1.0",
        "host_api_version": 5,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))
    (
        folder / "__init__.py"
    ).write_text("""from src.plugins import HOST_API_VERSION, PluginRegistration, PluginExtension, PluginMetadataSource
def register(context):
    return PluginRegistration(plugin_id="metadata_one", display_name="Metadata", version="1.0",
        host_api_version=HOST_API_VERSION, extensions=(PluginExtension(key="catalog.metadata_source",
        data=PluginMetadataSource(fetch_movie=lambda number: None)),))
""")
    with pytest.raises(PluginLoadError, match="manifest"):
        check_plugin_dir(plugin_dir=folder, plugin_settings=Plugins())
    manifest["host_api_version"] = 6
    (folder / "manifest.json").write_text(json.dumps(manifest))
    assert (
        check_plugin_dir(plugin_dir=folder, plugin_settings=Plugins()).plugin_id
        == "metadata_one"
    )


def test_concurrent_imports_keep_one_movie_and_its_images(
    metadata_env, monkeypatch, test_db
):
    barrier = Barrier(2)

    def fetch(_number):
        detail = delivery(metadata_env, request=uuid4().hex)
        barrier.wait(timeout=10)
        return detail

    register_sources(monkeypatch, {"metadata_one": fetch})

    def run_import():
        try:
            return MetadataSourceService.import_by_number(
                "TEST-001", metadata_env.provider, metadata_env.service
            )
        finally:
            if not test_db.is_closed():
                test_db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run_import(), range(2)))
    assert results[0][0].id == results[1][0].id
    assert sum(created for _, created in results) == 1
    assert Movie.select().count() == 1
    files = {
        p.relative_to(metadata_env.root / "assets").as_posix()
        for p in (metadata_env.root / "assets").rglob("*")
        if p.is_file()
    }
    from src.model import Image

    assert files == {image.origin for image in Image.select()}
    assert not list((metadata_env.root / "plugins").rglob("cover.png"))


def test_failed_plugin_import_cleans_its_delivery(metadata_env, monkeypatch):
    detail = delivery(metadata_env)
    register_sources(monkeypatch, {"metadata_one": Mock(return_value=detail)})
    monkeypatch.setattr(
        Movie, "create", Mock(side_effect=RuntimeError("insert failed"))
    )
    with pytest.raises(RuntimeError, match="insert failed"):
        MetadataSourceService.import_by_number(
            "TEST-001", metadata_env.provider, metadata_env.service
        )
    assert not Path(detail.cover_image_path).exists()
    assert not [p for p in (metadata_env.root / "assets").rglob("*") if p.is_file()]


def test_media_import_uses_fallback_and_subscribes_movie(metadata_env, monkeypatch):
    from src.service.transfers.imports.import_service import MediaImportService

    detail = delivery(metadata_env)
    register_sources(monkeypatch, {"metadata_one": Mock(return_value=detail)})
    service = MediaImportService(catalog_import_service=metadata_env.service)
    service._worker_local.metadata_provider = metadata_env.provider
    result = service._import_movie_metadata("TEST-001")
    movie = Movie.get_by_id(result.movie_id)
    assert movie.javdb_id is None and movie.is_subscribed
    assert movie.subscribed_at is not None


def test_ranking_hit_backfills_existing_plugin_movie(metadata_env, monkeypatch):
    from src.service.discovery.ranking_service import (
        RANKING_SOURCES,
        RankingBoardDefinition,
        RankingSourceDefinition,
        RankingSyncService,
    )

    movie = import_plugin(metadata_env, monkeypatch)
    monkeypatch.setitem(
        RANKING_SOURCES,
        "test_rank",
        RankingSourceDefinition(
            key="test_rank",
            name="Test",
            boards=(
                RankingBoardDefinition(
                    key="all",
                    name="All",
                    fetch_numbers=lambda _period: ["TEST-001"],
                ),
            ),
        ),
    )
    metadata_env.provider.get_movie_by_number.side_effect = None
    metadata_env.provider.get_movie_by_number.return_value = remote_detail()
    service = RankingSyncService(
        import_service=metadata_env.service,
        providers={"test_rank": metadata_env.provider},
    )
    service.sync_board_period("test_rank", "all", None)
    assert Movie.get_by_id(movie.id).javdb_id == "real-id"
    assert Movie.select().count() == 1
