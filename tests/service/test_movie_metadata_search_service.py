from datetime import date
from types import SimpleNamespace

import pytest
from PIL import Image as PillowImage

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.metadata._providers.models import JavdbMovieDetail
from src.model import Image, Movie
from src.plugins.extensions.metadata import PluginMetadataSource, PluginMovieMetadata
from src.service.catalog.metadata_source_service import MetadataSourceService
from src.service.catalog.movie_metadata_search_service import MovieMetadataSearchService


def _javdb_detail():
    return JavdbMovieDetail(
        javdb_id="javdb-001",
        movie_number="ABC-001",
        title="JavDB title",
        summary="",
        duration_minutes=120,
        actors=[],
        tags=[],
        cover_image="https://example.com/javdb-cover.jpg",
    )


def _plugin_detail(path):
    return PluginMovieMetadata(
        movie_number="ABC-001",
        title="Plugin title",
        release_date=date(2026, 9, 1),
        duration_minutes=121,
        cover_image_path=str(path),
        source_id="plugin-record-001",
    )


def test_manual_search_returns_javdb_and_all_enabled_plugin_candidates(
    test_db, tmp_path, monkeypatch
):
    image_root = tmp_path / "assets"
    plugin_root = tmp_path / "plugins"
    plugin_cover = (
        plugin_root
        / "metadata_one"
        / "data"
        / "metadata-tmp"
        / "request-one"
        / "cover.png"
    )
    plugin_cover.parent.mkdir(parents=True)
    PillowImage.new("RGB", (30, 20), "red").save(plugin_cover)
    calls = []

    def javdb_search(number):
        calls.append(("javdb", number))
        return _javdb_detail()

    def successful_plugin(number):
        calls.append(("metadata_one", number))
        return _plugin_detail(plugin_cover)

    def failed_plugin(_number):
        calls.append(("metadata_two", _number))
        raise RuntimeError("plugin offline")

    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    monkeypatch.setattr(settings.plugins, "root_dir", str(plugin_root))
    monkeypatch.setattr(settings.plugins, "enabled", ["metadata_one", "metadata_two"])
    monkeypatch.setattr(
        MetadataSourceService,
        "sources",
        (
            (
                "metadata_one",
                "Metadata One",
                PluginMetadataSource(
                    fetch_movie=successful_plugin
                ),
            ),
            (
                "metadata_two",
                "Metadata Two",
                PluginMetadataSource(
                    fetch_movie=failed_plugin
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.service.catalog.movie_metadata_search_service.build_javdb_provider",
        lambda: SimpleNamespace(get_movie_by_number=javdb_search),
    )

    class FakeImageService:
        def __init__(self):
            self.http_client = SimpleNamespace(close=lambda: None)
            self.image_downloader = self._download

        @staticmethod
        def _download(_url, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            PillowImage.new("RGB", (30, 20), "blue").save(target, format="JPEG")

    monkeypatch.setattr(
        "src.service.catalog.movie_metadata_search_service.MovieImageService",
        FakeImageService,
    )

    response = MovieMetadataSearchService.search_by_number(" abc-001 ")

    assert response.movie_number == "ABC-001"
    assert calls == [
        ("javdb", "ABC-001"),
        ("metadata_one", "ABC-001"),
        ("metadata_two", "ABC-001"),
    ]
    assert [candidate.source for candidate in response.candidates] == [
        "javdb",
        "plugin",
    ]
    assert response.candidates[0].cover_url.startswith("/files/images/metadata-search/")
    assert response.candidates[1].source_id == "plugin-record-001"
    assert len(response.source_errors) == 1
    assert response.source_errors[0].source == "metadata_two"
    assert not plugin_cover.exists()
    assert Movie.select().count() == 0
    assert Image.select().count() == 0

    cached_files = list((image_root / "metadata-search").rglob("*"))
    assert any(path.is_file() for path in cached_files)


def test_search_candidate_reference_rejects_disabled_plugin(monkeypatch):
    monkeypatch.setattr(settings.plugins, "enabled", [])
    monkeypatch.setattr(MetadataSourceService, "sources", ())

    with pytest.raises(ApiError) as exc_info:
        MovieMetadataSearchService.resolve_candidate_reference(
            "plugin:metadata_one:ABC-001"
        )

    assert exc_info.value.code == "invalid_metadata_candidate"
